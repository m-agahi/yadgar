"""Car F (task #61) — install-service printed message lint.

The ``yadgar daemon install-service`` CLI handler must print the
backend-first start command UNCONDITIONALLY (not "whichever is newer")
AND surface the ~101 s measured deploy window in the operator-facing
message. The ``Start:`` line itself is the dependency-ordered shell
command — backend && core — so a half-applied install can never come
up with the core running and the backend missing.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock


def test_install_service_handler_prints_backend_first() -> None:
    """The handler renders the Start line as ``start backend && start core``."""
    from yadgar.core.cli.daemon import _handle_install_service

    fake_daemon = MagicMock()
    fake_daemon.install_systemd_service.return_value = {
        "backend_service": "/home/max/.config/systemd/user/yadgar-backend.service",
        "core_service": "/home/max/.config/systemd/user/yadgar.service",
        "enable": "systemctl --user enable yadgar-backend.service yadgar.service",
        # The dependency-ordered start — backend FIRST.
        "start": (
            "systemctl --user start yadgar-backend.service && systemctl --user start yadgar.service"
        ),
        "status": "systemctl --user status yadgar-backend.service yadgar.service",
        "deploy_window_seconds": 101,
    }

    buf = io.StringIO()
    with redirect_stdout(buf):
        _handle_install_service(fake_daemon, dev=False)
    out = buf.getvalue()

    # Backend command appears BEFORE the core command on the Start line.
    start_line = next(ln for ln in out.splitlines() if ln.startswith("  Start:"))
    backend_pos = start_line.find("yadgar-backend.service")
    core_pos = start_line.find("yadgar.service", backend_pos + 1)
    assert backend_pos != -1, f"backend command missing on Start line: {start_line!r}"
    assert core_pos != -1, f"core command missing on Start line: {start_line!r}"
    assert backend_pos < core_pos, (
        f"backend must precede core on the Start line; got {start_line!r}"
    )


def test_install_service_handler_surfaces_deploy_window() -> None:
    """The ~101 s measured window is in the operator-facing message.

    Without the warning, an operator upgrading the install manually
    during the gap can restart the core before the backend is ready —
    the BindsTo+handshake safety net catches it, but a clear note in
    the printed message is what tells the operator WHY the gap exists.
    """
    from yadgar.core.cli.daemon import _handle_install_service

    fake_daemon = MagicMock()
    fake_daemon.install_systemd_service.return_value = {
        "backend_service": "/tmp/yadgar-backend.service",
        "core_service": "/tmp/yadgar.service",
        "enable": "systemctl --user enable yadgar-backend.service yadgar.service",
        "start": "systemctl --user start yadgar-backend.service",
        "status": "systemctl --user status yadgar-backend.service",
        "deploy_window_seconds": 101,
    }

    buf = io.StringIO()
    with redirect_stdout(buf):
        _handle_install_service(fake_daemon, dev=False)
    out = buf.getvalue()

    assert "101" in out, f"deploy window seconds (101) must be in the message: {out!r}"
    assert "window" in out.lower(), f"message must mention the deploy window: {out!r}"


def test_install_service_handler_omits_window_when_absent() -> None:
    """Older installs that pre-date the field skip the note rather than 0s."""
    from yadgar.core.cli.daemon import _handle_install_service

    fake_daemon = MagicMock()
    # No ``deploy_window_seconds`` — the handler must not crash and must
    # not print a "0 second window" note. Defensive: a malformed return
    # dict from an older install_systemd_service must degrade gracefully.
    fake_daemon.install_systemd_service.return_value = {
        "backend_service": "/tmp/yadgar-backend.service",
        "core_service": "/tmp/yadgar.service",
        "enable": "systemctl --user enable yadgar-backend.service yadgar.service",
        "start": "systemctl --user start yadgar-backend.service",
        "status": "systemctl --user status yadgar-backend.service",
    }

    buf = io.StringIO()
    with redirect_stdout(buf):
        _handle_install_service(fake_daemon, dev=False)
    out = buf.getvalue()

    # The other lines are still printed — only the window note is absent.
    assert "Backend:" in out
    assert "Enable:" in out
    # No spurious "0 second window" or similar.
    assert "0 second" not in out
    assert "0s window" not in out


def test_install_service_handler_does_not_crash_on_dev_true() -> None:
    """The dev arm produces the same message shape; no surprises from the dev path."""
    from yadgar.core.cli.daemon import _handle_install_service

    fake_daemon = MagicMock()
    fake_daemon.install_systemd_service.return_value = {
        "backend_service": "/tmp/yadgar-backend-dev.service",
        "core_service": "/tmp/yadgar-dev.service",
        "enable": "systemctl --user enable yadgar-backend-dev.service yadgar-dev.service",
        "start": (
            "systemctl --user start yadgar-backend-dev.service"
            " && systemctl --user start yadgar-dev.service"
        ),
        "status": "systemctl --user status yadgar-backend-dev.service yadgar-dev.service",
        "deploy_window_seconds": 101,
    }

    buf = io.StringIO()
    with redirect_stdout(buf):
        # No exception path — the handler accepts dev=True like dev=False.
        _handle_install_service(fake_daemon, dev=True)
    out = buf.getvalue()

    assert "yadgar-dev.service" in out
    assert "101" in out
