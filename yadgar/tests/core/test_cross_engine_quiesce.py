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

# Captured at IMPORT time, before the autouse fixture below replaces the
# attribute. The detection tests at the bottom of this file need the genuine
# implementation back; ``wraps=quiesce._engine_two_state`` cannot give it to
# them, because by the time a test body runs that name already resolves to the
# fixture's stand-in and the "real" detector would quietly be the fake one.
_REAL_ENGINE_TWO_STATE = quiesce._engine_two_state


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


@pytest.fixture(autouse=True)
def _engine_two_present_by_default():
    """Every pre-existing case in this file is about an engine that IS there.

    The driver now asks the backend whether engine #2 exists BEFORE it asserts
    the gate, so without this each of the fakes below would have to answer one
    more op. Pinning the detector to PRESENT keeps those cases testing what they
    were written to test — the gate, the drain, the artifact — while the
    detection tests at the bottom of this file patch it back out and drive the
    real ``_engine_two_state`` through ``_forward_admin``.

    Deliberately NOT the default the production code takes when it cannot tell:
    that is a hard failure, and it has its own tests.
    """
    # A stand-in already in place would mean this fixture is nesting over
    # another patch rather than over the real function — the shape that made
    # ``wraps=quiesce._engine_two_state`` silently wrap the FAKE and turned four
    # detection tests green against a detector that never ran. Assert the thing
    # being replaced is the genuine article so that bug cannot return silently.
    assert quiesce._engine_two_state is _REAL_ENGINE_TWO_STATE, (
        "the detector was already patched before this fixture ran — a nested "
        "stand-in makes every test in this file assert against a fake detector"
    )
    with patch.object(
        quiesce, "_engine_two_state", return_value=(quiesce.ENGINE_PRESENT, "test-default")
    ):
        yield


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


# ---------------------------------------------------------------------------
# Engine #2 ABSENT -> clean SKIP; "cannot tell" -> hard fail
#
# The nightly runs host-side and cannot see engine #2 for itself, so step 5b
# asks the backend. Getting the QUESTION right matters more than getting the
# answer right, because the two wrong answers fail in OPPOSITE directions:
#
#   * calling a PRESENT engine absent silently disables the backup — the arm
#     stops running and every log line still reads green. That is the
#     vacuous-pass class this train exists to close.
#   * calling an ABSENT engine present hard-fails step 5b on every host that has
#     not deployed the new backend image, which is the defect being fixed.
#
# So absence is only ever concluded from a POSITIVE answer by a REACHABLE
# backend. Anything that merely fails to answer — a connect error, a 5xx, a
# malformed body — is "cannot tell", and cannot-tell fails CLOSED.
# ---------------------------------------------------------------------------


def _http_error(status_code: int, body: str) -> Exception:
    """An ``httpx.HTTPStatusError`` shaped exactly as ``_forward_admin`` raises it.

    ``_forward_admin`` calls ``resp.raise_for_status()``, so its caller sees a
    real ``HTTPStatusError`` carrying the response. Building the genuine article
    rather than a stand-in keeps these tests honest about the ``.response``
    attribute the detector reads.
    """
    import httpx

    request = httpx.Request("POST", "http://127.0.0.1:8001/admin")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def _detect_only(forward):
    """Patch the driver so ONLY detection can run: the gate raises if reached.

    The ordering is the contract, not an implementation detail — an absent
    engine must never cause a maintenance window, and the window IS a full MCP
    outage (ADR-0210). Making ``_maintenance_enter`` explode is what turns
    "detect first, gate second" from a comment into an assertion.
    """
    return (
        patch.object(quiesce, "_engine_two_state", _REAL_ENGINE_TWO_STATE),
        patch.object(quiesce, "_forward_admin", side_effect=forward),
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=AssertionError("the gate was asserted despite an unresolved engine"),
        ),
        patch.object(
            quiesce,
            "create_snapshot",
            side_effect=AssertionError("a snapshot was taken despite an unresolved engine"),
        ),
    )


def _status_only(present):
    """A ``_forward_admin`` fake that answers the status op and nothing else."""

    def _forward(op, payload, timeout_s=30.0):
        assert op == "sql_engine_status", f"unexpected op before detection resolved: {op}"
        return {"present": present, "engine": "mariadb"}

    return _forward


def test_engine_absent_skips_and_never_asserts_the_gate(tmp_path):
    """The backend says there is no engine #2 -> skip cleanly, open NO window.

    There is nothing to quiesce, so there is nothing to fail about. The gate
    stub raises if touched: an absent engine must not cost an MCP outage.
    """
    detect, forward, gate, snap = _detect_only(_status_only(False))
    with detect, forward, gate, snap:
        result = _run(tmp_path)

    assert result["skipped"] is True
    assert result["ok"] is True
    assert result["reason"] == quiesce.REASON_ENGINE_TWO_ABSENT
    # A skip is not a backup that produced nothing: it carries no artifact keys
    # at all, so a caller cannot log it as a dump-less success.
    assert "sql_dump" not in result
    assert "surreal_snapshot" not in result


def test_old_backend_image_that_does_not_know_the_op_is_absence(tmp_path):
    """A 400 ``unknown admin op`` from a REACHABLE backend proves engine #2 is absent.

    The image that registers ``sql_engine_status`` is the same image that bakes
    mariadb-server (ADR-0212), so a backend that does not know the op cannot be
    running engine #2. This is the skew that hard-failed the nightly on every
    host without the new backend image — and it is a POSITIVE answer, not
    silence: an unreachable backend cannot return 400.
    """

    def _forward(op, payload, timeout_s=30.0):
        raise _http_error(400, "\"unknown admin op: 'sql_engine_status'\"")

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap:
        result = _run(tmp_path)

    assert result["skipped"] is True
    assert result["reason"] == quiesce.REASON_BACKEND_PREDATES_ENGINE_TWO


# -- the discriminator: "cannot tell" is NOT absence -------------------------


def test_unreachable_backend_is_not_absence(tmp_path):
    """THE mutation guard. A detector that answered "absent" here would silently
    disable the backup on every host whose backend is merely down or slow.

    An engine that is present-but-unreachable is indistinguishable from an
    absent one ONLY if silence counts as an answer. Here it does not: no answer
    is a hard failure, and no snapshot is taken either way.
    """
    import httpx

    def _forward(op, payload, timeout_s=30.0):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap, pytest.raises(RuntimeError, match="could not determine"):
        _run(tmp_path)


def test_backend_error_response_is_not_absence(tmp_path):
    """A 500 means the backend broke while answering, not that the engine is gone.

    Distinct from the 400 case ON PURPOSE. Without this, the unknown-op branch
    could be widened to catch every ``HTTPStatusError`` and nothing would
    notice — turning any backend fault into a silently skipped backup.
    """

    def _forward(op, payload, timeout_s=30.0):
        raise _http_error(500, "internal server error")

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap, pytest.raises(RuntimeError, match="could not determine"):
        _run(tmp_path)


def test_a_400_that_is_not_about_an_unknown_op_is_not_absence(tmp_path):
    """400 alone is not the signal — the ``unknown admin op`` detail is.

    A validation rejection is the backend refusing THIS request, not reporting
    on engine #2. Reading any 400 as absence would make a malformed payload look
    like a missing engine.
    """

    def _forward(op, payload, timeout_s=30.0):
        raise _http_error(400, '{"detail":"payload failed validation"}')

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap, pytest.raises(RuntimeError, match="could not determine"):
        _run(tmp_path)


def test_a_malformed_status_body_is_not_absence(tmp_path):
    """A response missing ``present`` answered nothing. Absence must be EXPLICIT.

    Guards the ``present is False`` shape: a falsy default would read an empty
    body as "absent", inferring a state-changing conclusion from a key that is
    not there — the 2026-06-16 shape.
    """

    def _forward(op, payload, timeout_s=30.0):
        return {"engine": "mariadb"}

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap, pytest.raises(RuntimeError, match="could not determine"):
        _run(tmp_path)


def test_no_backend_url_configured_is_not_absence(tmp_path):
    """``_forward_admin`` raises when YADGAR_EMBED_URL is unset — a
    misconfiguration, not a verdict about engine #2."""

    def _forward(op, payload, timeout_s=30.0):
        raise RuntimeError("YADGAR_EMBED_URL is not set")

    detect, forward, gate, snap = _detect_only(_forward)
    with detect, forward, gate, snap, pytest.raises(RuntimeError, match="could not determine"):
        _run(tmp_path)


# -- present: none of the three hard failures changes ------------------------


def test_detection_precedes_the_gate_on_the_happy_path(tmp_path):
    """Order, asserted as an order: ask FIRST, gate SECOND.

    Detection must not need a window — a window is a full MCP outage, so opening
    one only to discover there was nothing to back up is the cost this fix
    exists to avoid.
    """
    rec = _Recorder()
    filename = "mariadb.yadgar.q-present.sql"

    def _forward(op, payload, timeout_s=30.0):
        rec.calls.append(f"forward:{op}")
        if op == "sql_engine_status":
            return {"present": True, "engine": "mariadb"}
        if op == "drain_now":
            return {"drained": True, "items_processed": 0}
        if op == "mariadb_restore_verify":
            return _verify_ok(filename)
        _plant_dump(tmp_path, filename)
        return _dump_result(filename)

    def _enter(ttl):
        rec.calls.append("enter")
        return {"previous": True, "deadline_seconds": 900.0}

    def _snapshot(db_path, snapshot_dir, label, backend_url):
        rec.calls.append(f"surreal:{label}")
        return Path("/x.surql")

    with (
        patch.object(quiesce, "_engine_two_state", _REAL_ENGINE_TWO_STATE),
        patch.object(quiesce, "_forward_admin", side_effect=_forward),
        patch.object(quiesce, "_maintenance_enter", side_effect=_enter),
        patch.object(quiesce, "create_snapshot", side_effect=_snapshot),
    ):
        result = _run(tmp_path)

    assert rec.calls == [
        "forward:sql_engine_status",
        "enter",
        "forward:drain_now",
        "forward:mariadb_dump",
        "forward:mariadb_restore_verify",
        "surreal:quiesce",
    ]
    assert result["ok"] is True
    assert result.get("skipped", False) is False


def test_present_engine_still_hard_fails_on_an_unobtainable_gate(tmp_path):
    """ADR-0210 §3 is untouched by the skip path: a real engine + no gate = raise.

    The skip must be reachable ONLY through absence. If a present engine could
    reach it, this fix would have weakened the three hard failures rather than
    scoping them.
    """
    with (
        patch.object(
            quiesce, "_engine_two_state", return_value=(quiesce.ENGINE_PRESENT, "present")
        ),
        patch.object(quiesce, "_maintenance_enter", side_effect=ConnectionError("core down")),
        patch.object(quiesce, "create_snapshot", side_effect=AssertionError("snapshotted")),
        pytest.raises(RuntimeError, match="write-gate"),
    ):
        _run(tmp_path)


def test_present_engine_still_hard_fails_on_a_null_deadline(tmp_path):
    """The belt check (ADR-0211) is likewise untouched for a present engine."""
    with (
        patch.object(
            quiesce, "_engine_two_state", return_value=(quiesce.ENGINE_PRESENT, "present")
        ),
        patch.object(
            quiesce,
            "_maintenance_enter",
            side_effect=lambda ttl: {"previous": True, "deadline_seconds": None},
        ),
        patch.object(quiesce, "create_snapshot", side_effect=AssertionError("snapshotted")),
        pytest.raises(RuntimeError, match="deadline_seconds"),
    ):
        _run(tmp_path)
