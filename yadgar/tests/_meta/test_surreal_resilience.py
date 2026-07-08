"""Regression tests for the SurrealDB liveness/respawn resilience layer.

Guards the fix for the xdist cascade: when one worker's surreal subprocess dies
mid-run, the function-scoped ``_surreal_liveness`` gate must respawn it in place
(same port) instead of letting every subsequent test ERROR with ConnectError.

These tests drive ``_ensure_surreal_alive`` directly against a DEDICATED surreal
instance (not the shared session server) so they are deterministic and do not
pollute other tests on the worker.
"""

from __future__ import annotations

import shutil

import pytest

from yadgar.tests import conftest
from yadgar.tests._surreal_helpers import spawn_surreal, teardown_surreal_proc
from yadgar.tests.conftest import (
    _MAX_SURREAL_RESPAWNS,
    _ensure_surreal_alive,
    _find_free_port,
    _wait_for_health,
)

_requires_surreal = pytest.mark.skipif(
    not shutil.which("surreal"), reason="surreal binary not on PATH"
)


@_requires_surreal
def test_ensure_alive_noop_when_running(tmp_path):
    """Healthy server → no respawn, returns False, proc unchanged."""
    port = _find_free_port()
    data_dir = tmp_path / "live"
    data_dir.mkdir()
    proc = spawn_surreal(port=port, data_dir=str(data_dir))
    handle = {"proc": proc, "port": port, "data_dir": str(data_dir), "respawns": 0}
    try:
        _wait_for_health(port)
        assert _ensure_surreal_alive(handle) is False
        assert handle["proc"] is proc
        assert handle["respawns"] == 0
    finally:
        teardown_surreal_proc(handle["proc"], wait_timeout=5)


@_requires_surreal
def test_respawn_after_death_recovers_on_same_port(tmp_path):
    """Dead server → respawn on same port, fresh data dir, reachable again."""
    port = _find_free_port()
    data_dir = tmp_path / "dead"
    data_dir.mkdir()
    proc = spawn_surreal(port=port, data_dir=str(data_dir))
    handle = {"proc": proc, "port": port, "data_dir": str(data_dir), "respawns": 0}
    try:
        _wait_for_health(port)

        # Simulate an OOM SIGKILL of the worker's surreal.
        proc.kill()
        proc.wait(timeout=5)
        assert proc.poll() is not None

        respawned = _ensure_surreal_alive(handle)

        assert respawned is True
        assert handle["respawns"] == 1
        assert handle["proc"] is not proc
        assert handle["proc"].poll() is None  # new proc alive
        assert handle["port"] == port  # SAME port → YADGAR_DB_URL stays valid
        assert handle["data_dir"] != str(data_dir)  # fresh dir (old store corrupt)
        # Behavioral: the server answers on the same port after recovery.
        _wait_for_health(port)
    finally:
        teardown_surreal_proc(handle["proc"], wait_timeout=5)


def test_respawn_cap_raises(monkeypatch):
    """Past the respawn cap, raise loudly instead of masking a cascade."""

    class _DeadProc:
        def poll(self):
            return 1  # always dead

    handle = {
        "proc": _DeadProc(),
        "port": 0,
        "data_dir": "/nonexistent",
        "respawns": _MAX_SURREAL_RESPAWNS,  # next attempt exceeds the cap
    }
    # Never actually spawn or health-check — we must raise before that.
    monkeypatch.setattr(
        "yadgar.tests._surreal_helpers.spawn_surreal",
        lambda **kw: pytest.fail("should not respawn past cap"),
    )
    monkeypatch.setattr(conftest, "_wait_for_health", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="infra.*unstable"):
        _ensure_surreal_alive(handle)
