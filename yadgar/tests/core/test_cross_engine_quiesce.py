"""Engine-#2 car F: the cross-engine backup quiesce (ADR-0204 as amended by ADR-0210/0211).

The sequence under test: assert the gate -> VERIFIED-EMPTY drain -> snapshot
MariaDB -> snapshot Surreal -> release. Order is the whole point: an undrained
queue means writes in flight that belong to neither snapshot, and a MariaDB row
points one way at a Surreal body page (ADR-0204 context), so the two artifacts
have to describe one instant.

Three hard-fail rules, all of which the 2026-06-16 shape argues for — that
incident was a partial restore that PASSED its check:

  * the gate cannot be held        -> no snapshot. Explicitly UNLIKE nightly's
    own best-effort entry (``nightly_cycle.py`` step 1 proceeds ungated), which
    degrades a maintenance pass; a backup proceeding ungated is silently
    inconsistent.
  * ``deadline_seconds`` is null   -> no snapshot. Car E returns it precisely so
    a holder can VERIFY it has a self-heal belt (ADR-0211); null means it does
    not have one.
  * the drain is not VERIFIED empty -> no snapshot. ``drain_now`` returns
    ``{"drained": False, "items_processed": 0}`` both when it worked on nothing
    and when NO LIVE DRAINER IS WIRED (``backend/admin_exec/drain.py:44-50``),
    so ``items_processed == 0`` alone passes vacuously against a backend that
    cannot drain at all. ``drained is True`` is the half that carries evidence.

Nesting: the driver runs INSIDE nightly's window, so its enter returns
``previous=True`` and it must NOT exit — same caller-side contract vacuum
consumes at ``core/vacuum/__init__.py:1869``. The gate primitive itself is
untouched (ADR-0211).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from yadgar.core.backup import quiesce


def _dump_result(filename: str = "mariadb.yadgar.nightly-quiesce-20260806T210000Z.sql") -> dict:
    return {"ok": True, "filename": filename, "bytes": 42, "database": "yadgar"}


def _verify_ok(filename: str = "x.sql") -> dict:
    """Car G's enumeration reporting a clean restore.

    The driver treats anything other than ``status == "ok"`` as a hard failure,
    so every fake that gets as far as the dump has to answer this op — an
    unanswered one would look exactly like a verification that refused.
    """
    return {
        "ok": True,
        "status": "ok",
        "artifact": filename,
        "violations": [],
        "unavailable": [],
        "checks": {"row_identity": {"status": "ok", "detail": {"counts": {"config": {}}}}},
    }


def _plant_dump(root: Path, filename: str, body: str = "CREATE TABLE `config` (id INT);\n") -> Path:
    target = root / "backups" / "mariadb" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


class _Recorder:
    """Records the ordered sequence of side effects the driver performs."""

    def __init__(self) -> None:
        self.calls: list[str] = []


@pytest.fixture
def harness(tmp_path):
    """A driver wired to fakes: nested window, clean drain, planted dump, fake export."""
    rec = _Recorder()
    filename = "mariadb.yadgar.nightly-quiesce-20260806T210000Z.sql"

    def _fake_enter(ttl):
        rec.calls.append("enter")
        return {"previous": True, "deadline_seconds": 21000.0}

    def _fake_exit():
        rec.calls.append("exit")

    def _fake_forward(op, payload, timeout_s=30.0):
        rec.calls.append(f"forward:{op}")
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        if op == "mariadb_dump":
            _plant_dump(tmp_path, filename)
            return _dump_result(filename)
        if op == "mariadb_restore_verify":
            return _verify_ok(filename)
        raise AssertionError(f"unexpected op {op}")

    def _fake_snapshot(db_path, snapshot_dir, label, backend_url):
        rec.calls.append(f"surreal:{label}")
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        target = snapshot_dir / f"surreal_db.{label}-20260806T210000Z.surql"
        target.write_text("-- export", encoding="utf-8")
        return target

    with (
        patch.object(quiesce, "_maintenance_enter", side_effect=_fake_enter),
        patch.object(quiesce, "_maintenance_exit", side_effect=_fake_exit),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=_fake_snapshot),
    ):
        yield rec, tmp_path, filename


def _run(root: Path, **kwargs):
    return quiesce.run_cross_engine_backup(
        db_path=root / "surreal_db",
        snapshot_dir=root / "backups" / "surql",
        backend_url="http://127.0.0.1:8000",
        data_root=root,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy path — ordering
# ---------------------------------------------------------------------------


def test_sequence_is_gate_drain_mariadb_surreal_release(harness):
    """ADR-0204's order, asserted as an order and not just as a set of calls."""
    rec, root, filename = harness
    result = _run(root)

    assert rec.calls == [
        "enter",
        "forward:drain_now",
        "forward:mariadb_dump",
        # Car G: the artifact is PROVEN RESTORABLE before the Surreal half is
        # taken, so a dump that cannot be restored never gets a partner.
        "forward:mariadb_restore_verify",
        "surreal:quiesce",
    ]
    assert result["ok"] is True
    assert result["sql_dump"] == filename
    assert result["surreal_snapshot"].endswith(".surql")


def test_nested_window_is_not_released_on_exit(harness):
    """previous=True means an outer holder (nightly) owns the window — do NOT un-gate it."""
    rec, root, _ = harness
    _run(root)
    assert "exit" not in rec.calls


def test_own_window_is_released(tmp_path):
    """previous=False means WE opened it — the release-on-abort belt must fire."""
    rec = _Recorder()
    filename = "mariadb.yadgar.q-1.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": False, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_maintenance_exit", side_effect=lambda: rec.calls.append("exit")),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(
            quiesce,
            "create_snapshot",
            side_effect=lambda db_path, snapshot_dir, label, backend_url: Path("/x.surql"),
        ),
    ):
        _run(tmp_path)

    assert rec.calls == ["exit"]


# ---------------------------------------------------------------------------
# Hard-fail: the gate
# ---------------------------------------------------------------------------


def test_gate_unobtainable_hard_fails_with_no_snapshot(tmp_path):
    """Core unreachable -> RuntimeError, and NEITHER engine is snapshotted."""
    touched: list[str] = []

    with (
        patch.object(
            quiesce, "_maintenance_enter", side_effect=ConnectionError("core unreachable")
        ),
        patch.object(quiesce, "_forward_admin", side_effect=lambda *a, **k: touched.append("fwd")),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="write-gate"),
    ):
        _run(tmp_path)

    assert touched == []


def test_null_deadline_hard_fails_with_no_snapshot(tmp_path):
    """No belt (deadline_seconds is null) is a hard failure — ADR-0211 pairs it with 0210."""
    touched: list[str] = []

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": None},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=lambda *a, **k: touched.append("fwd")),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="deadline_seconds"),
    ):
        _run(tmp_path)

    assert touched == []


def test_null_deadline_releases_a_window_we_opened(tmp_path):
    """Hard-failing must not leave OUR window wedged — release before raising."""
    rec = _Recorder()

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": False, "deadline_seconds": None},
        ),
        patch.object(quiesce, "_maintenance_exit", side_effect=lambda: rec.calls.append("exit")),
        pytest.raises(RuntimeError),
    ):
        _run(tmp_path)

    assert rec.calls == ["exit"]


# ---------------------------------------------------------------------------
# Hard-fail: the drain
# ---------------------------------------------------------------------------


def test_drain_that_reports_no_live_drainer_hard_fails(tmp_path):
    """drained=False + items_processed=0 is 'could not drain', NOT 'queue empty'."""
    touched: list[str] = []

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": False, "items_processed": 0}
        touched.append(op)
        return {}

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="drain"),
    ):
        _run(tmp_path)

    assert touched == []


def test_drain_retries_until_verified_empty(tmp_path):
    """A non-empty first pass is fine — re-drain until a pass reports zero items."""
    passes = iter(
        [
            {"drained": True, "items_processed": 4},
            {"drained": True, "items_processed": 1},
            {"drained": True, "items_processed": 0},
        ]
    )
    filename = "mariadb.yadgar.q-2.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return next(passes)
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(
            quiesce,
            "create_snapshot",
            side_effect=lambda db_path, snapshot_dir, label, backend_url: Path("/x.surql"),
        ),
    ):
        result = _run(tmp_path)

    assert result["drain_passes"] == 3


def test_drain_that_never_settles_hard_fails(tmp_path):
    """A queue that keeps refilling is a free-running writer — refuse to snapshot it."""
    touched: list[str] = []

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 7}
        touched.append(op)
        return {}

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="never settled"),
    ):
        _run(tmp_path, max_drain_passes=3)

    assert touched == []


# ---------------------------------------------------------------------------
# Hard-fail: the artifact itself
# ---------------------------------------------------------------------------


def test_dump_missing_on_the_hosts_side_hard_fails(tmp_path):
    """The op reported success but nothing landed under the shared root — the trap."""
    touched: list[str] = []

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        return _dump_result("mariadb.yadgar.ghost.sql")  # nothing planted

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="not visible"),
    ):
        _run(tmp_path)

    assert touched == []


def test_dump_without_the_expected_table_hard_fails(tmp_path):
    """Zero rows makes 'succeeded' and 'empty' look alike — assert the schema is in there."""
    filename = "mariadb.yadgar.q-3.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        _plant_dump(tmp_path, filename, body="-- MariaDB dump\n-- no tables at all\n")
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        pytest.raises(RuntimeError, match="config"),
    ):
        _run(tmp_path)

    # An artifact that failed verification must not sit in the retention pool
    # looking like a good backup to anything that only globs the pattern.
    assert not (tmp_path / "backups" / "mariadb" / filename).exists()


def test_a_failed_surreal_half_removes_the_orphaned_dump(tmp_path):
    """The two halves age in SEPARATE retention pools — an orphan skews them apart.

    ``_step_prune`` retains N of each pool independently, so a dump kept without
    its partner both wastes a slot and leaves car G an artifact it cannot pair.
    Keeping the pools 1:1 by construction beats arithmetic that holds only while
    nothing fails.
    """
    filename = "mariadb.yadgar.q-5.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=RuntimeError("export 500")),
        pytest.raises(RuntimeError, match="export 500"),
    ):
        _run(tmp_path)

    assert not (tmp_path / "backups" / "mariadb" / filename).exists()


def test_release_still_happens_when_the_surreal_snapshot_raises(tmp_path):
    """Release-on-abort: a failure AFTER the gate is held must not wedge writes."""
    rec = _Recorder()
    filename = "mariadb.yadgar.q-4.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "mariadb_restore_verify":
            return _verify_ok()
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": False, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_maintenance_exit", side_effect=lambda: rec.calls.append("exit")),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=RuntimeError("export 500")),
        pytest.raises(RuntimeError, match="export 500"),
    ):
        _run(tmp_path)

    assert rec.calls == ["exit"]


# ---------------------------------------------------------------------------
# Car G: the artifact must be PROVEN RESTORABLE before it is kept
# ---------------------------------------------------------------------------


def test_a_refused_restore_verification_deletes_the_dump_and_takes_no_surreal_half(tmp_path):
    """A dump that does not restore is worse than no dump — it reads as a backup.

    2026-06-16's real damage was trusting a restore that had not been verified.
    So a refusal here removes the artifact rather than leaving it in the retention
    pool, and the Surreal half is never taken: the two pools stay 1:1 by
    construction rather than by arithmetic that only holds while nothing fails.
    """
    filename = "mariadb.yadgar.q-6.sql"
    touched: list[str] = []

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        if op == "mariadb_restore_verify":
            return {
                "status": "violation",
                "violations": ["restore[row_identity]: 2 rows missing"],
                "unavailable": [],
                "checks": {},
            }
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: touched.append("snap")),
        pytest.raises(RuntimeError, match="did NOT verify by enumeration"),
    ):
        _run(tmp_path)

    assert touched == []
    assert not (tmp_path / "backups" / "mariadb" / filename).exists()


def test_an_unavailable_verification_is_also_refused(tmp_path):
    """``unavailable`` is not ``ok``. A check that could not run proves nothing.

    Distinct from the violation case on purpose: a driver that only tested for
    ``status == "violation"`` would keep an artifact whose verification never
    ran, which is the exact vacuous-pass shape car H's tri-state exists for.
    """
    filename = "mariadb.yadgar.q-7.sql"

    def _fake_forward(op, payload, timeout_s=30.0):
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        if op == "mariadb_restore_verify":
            return {
                "status": "unavailable",
                "violations": [],
                "unavailable": ["row_identity(query_failed)"],
                "checks": {},
            }
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    with (
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": 900.0},
        ),
        patch.object(quiesce, "_forward_admin", side_effect=_fake_forward),
        patch.object(quiesce, "create_snapshot", side_effect=lambda **k: None),
        pytest.raises(RuntimeError, match="did NOT verify by enumeration"),
    ):
        _run(tmp_path)

    assert not (tmp_path / "backups" / "mariadb" / filename).exists()


def test_verify_dump_streams_rather_than_slurping_the_artifact(tmp_path, monkeypatch):
    """``_verify_dump`` must never read the whole artifact into memory.

    The first cut called ``read_text()``. Harmless against a zero-row table and
    wrong the moment engine #2 holds real data — a logical dump has no size bound
    and this runs on the nightly host beside everything else.

    TWO pins, because the obvious one alone is too narrow. Making ``read_text``
    explode only proves that one method is not called; it says nothing about a
    loop that appends every line to a list, and against the one-line artifact the
    other tests plant, streaming and slurping are indistinguishable anyway. So
    the artifact here carries a 5,000-line TAIL after the marker and the lines
    actually consumed are counted: stopping at the marker is the property, and it
    is measured rather than asserted by construction.
    """

    def _boom(*args, **kwargs):
        raise AssertionError("_verify_dump slurped the artifact instead of streaming it")

    consumed = [0]
    real_open = Path.open

    class _CountingFile:
        """A handle that records how many lines the caller actually pulls."""

        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

        def __iter__(self):
            for line in self._handle:
                consumed[0] += 1
                yield line

    planted = _plant_dump(
        tmp_path,
        "mariadb.yadgar.q-8.sql",
        body="CREATE TABLE `config` (id INT);\n" + "-- filler\n" * 5000,
    )

    def _counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return _CountingFile(handle) if self == planted else handle

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "open", _counting_open)

    assert quiesce._verify_dump(tmp_path, planted.name, "config") == planted
    # The marker is line 1 of 5,001. Anything that walks the whole artifact —
    # or accumulates it — shows up here as a number in the thousands.
    assert consumed[0] == 1
