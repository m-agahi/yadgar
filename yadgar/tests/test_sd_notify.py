import socket
import threading
import time

import pytest

from yadgar import sd_notify


@pytest.fixture
def fake_systemd_socket(tmp_path):
    """Spawn a thread-backed AF_UNIX SOCK_DGRAM listener on a tmp path.
    Yields (sock_path, get_received_payloads_callable).
    """
    sock_path = str(tmp_path / "notify.sock")
    received: list[bytes] = []
    stop = threading.Event()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(0.5)

    def listen():
        while not stop.is_set():
            try:
                data, _ = server.recvfrom(4096)
                received.append(data)
            except TimeoutError:
                continue
            except OSError:
                break

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    try:
        yield sock_path, lambda: list(received)
    finally:
        stop.set()
        server.close()
        t.join(timeout=1)


def test_notify_ready_writes_to_socket(fake_systemd_socket, monkeypatch):
    sock_path, get_received = fake_systemd_socket
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    result = sd_notify.ready()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not get_received():
        time.sleep(0.01)
    assert result is True
    assert get_received() == [b"READY=1"]


def test_notify_stopping_writes_to_socket(fake_systemd_socket, monkeypatch):
    sock_path, get_received = fake_systemd_socket
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    result = sd_notify.stopping()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not get_received():
        time.sleep(0.01)
    assert result is True
    assert get_received() == [b"STOPPING=1"]


def test_notify_no_socket_env_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    result = sd_notify.ready()

    assert result is False
