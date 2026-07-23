"""Regression: the daemon deployment paths MUST wire the shared file-queue.

Bug: `yadgar daemon start` (start_backend) and `yadgar daemon install-service`
(systemd generator) omitted YADGAR_QUEUE_BASE and the shared queue-volume mount
on the backend. Core writes the async write-queue to {YADGAR_DATA_DIR}/queue on
its own volume; the backend drainer reads YADGAR_QUEUE_BASE with NO fallback
(embed_service_lifecycle._queue_base_path). Unset → drainer disabled → every
queued memorize/wiki_add write silently never commits. Only docker-compose wired
it. These tests pin both code paths so the regression cannot return.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from yadgar.core.daemon import daemon as daemon_mod
from yadgar.core.daemon import systemd as systemd_mod
from yadgar.core.daemon.profiles import _prod_profile


def _argv_from_calls(calls: list[list[str]]) -> list[str]:
    """Return the `docker run` argv (the call containing 'run' and the backend name)."""
    for cmd in calls:
        if "run" in cmd and any("yadgar-backend" in str(a) for a in cmd):
            return cmd
    raise AssertionError(f"no backend `docker run` argv captured; calls={calls}")


def test_start_backend_wires_shared_queue_volume(monkeypatch):
    """start_backend's docker-run argv must mount the core volume at /queue-data
    and set YADGAR_QUEUE_BASE=/queue-data (mirrors docker-compose.yml)."""
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")
    monkeypatch.delenv("YADGAR_BACKEND_VOLUME", raising=False)

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="fakecontainerid", stderr="")

    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_mod, "_ensure_network", lambda: None)
    monkeypatch.setattr(daemon_mod, "_get_runtime", lambda: "docker")
    monkeypatch.setattr(daemon_mod, "_container_memory_mb", lambda: 512)
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda *_: None)

    d = daemon_mod.YadgarDaemon()
    # Not running before start (top guard); "not running" in the health loop makes
    # start_backend return a failure — but the run argv is already captured by then.
    monkeypatch.setattr(d, "_container_running", lambda *a, **k: False)
    monkeypatch.setattr(d, "_image_exists", lambda *a, **k: True)

    d.start_backend()

    argv = _argv_from_calls(calls)
    assert "YADGAR_QUEUE_BASE=/queue-data" in argv, (
        "backend docker-run missing `-e YADGAR_QUEUE_BASE=/queue-data` — "
        "drainer will be disabled and queued writes never commit."
    )
    assert "yadgar-data:/queue-data" in argv, (
        "backend docker-run missing `-v yadgar-data:/queue-data` — the drainer "
        "cannot see the core's queue without the shared volume mount."
    )


def test_systemd_backend_unit_wires_shared_queue_volume(monkeypatch, tmp_path):
    """The generated yadgar-backend.service must carry the queue volume + QUEUE_BASE."""
    # Redirect HOME so we never touch the operator's real ~/.config/systemd/user.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("YADGAR_VOLUME", "yadgar-data")

    profile = _prod_profile(8765)
    result = systemd_mod.install_systemd_service(profile, dev=False)

    backend_unit = Path(result["backend_service"]).read_text()
    assert "YADGAR_QUEUE_BASE=/queue-data" in backend_unit, (
        "generated yadgar-backend.service missing YADGAR_QUEUE_BASE=/queue-data."
    )
    assert f"{profile.volume_name}:/queue-data" in backend_unit, (
        "generated yadgar-backend.service missing the shared queue-volume mount "
        f"`{profile.volume_name}:/queue-data`."
    )
