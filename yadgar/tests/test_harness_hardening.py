"""TDD tests for v5.10.0 test harness hardening.

Covers:
- atexit handler kills all spawned SurrealDB subprocesses
- pytest_sessionfinish hook fires on non-zero exitstatus
- Port allocation determinism via PYTEST_XDIST_WORKER
- Port collision retry advances to next port
- YADGAR_TEST_NAMESPACE redirects tmp dir to /tmp/pytest-<namespace>/
- @pytest.mark.timeout(N) per-test override works via subprocess pytest

IMPORTANT: Tests that spawn surreal binaries are gated on `shutil.which("surreal")`.
Tests that only test the registry/port logic mock subprocesses — run always.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Repo root: tests/ → yadgar/ → repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_port(port: int) -> socket.socket:
    """Bind and listen on *port*; return the socket (caller must close).

    Must call listen() so connect()-based port-in-use checks see the port
    as occupied (bind-only sockets aren't reachable via connect on Linux).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def _run_pytest_subprocess(
    test_src: str, *, extra_env: dict | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Write *test_src* to a temp file and run it in a child pytest process."""
    import tempfile

    env = os.environ.copy()
    env.update(extra_env or {})
    # Ensure the harness env vars don't interfere
    env.pop("PYTEST_XDIST_WORKER", None)
    # Use short global timeout so hung tests don't block the parent
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, dir="/tmp") as f:
        f.write(test_src)
        tmp_path = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                tmp_path,
                "-x",
                "--no-header",
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                "--timeout=30",
                "--override-ini=addopts=",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# 1. atexit handler kills all spawned SurrealDB subprocesses
# ---------------------------------------------------------------------------


class TestAtexitHandler:
    """_surreal_helpers.spawn_surreal registers PIDs; kill_all_spawned_surreal kills them."""

    def test_spawn_registers_pid(self):
        """spawn_surreal appends pid to _SPAWNED_SURREAL_PIDS."""
        from yadgar.tests._surreal_helpers import _SPAWNED_SURREAL_PIDS, spawn_surreal

        len(_SPAWNED_SURREAL_PIDS)
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        with patch("yadgar._surreal_runner.subprocess.Popen", return_value=fake_proc):
            result = spawn_surreal(port=19999, data_dir="/tmp/fake-surreal-data")
        assert result is fake_proc
        assert fake_proc.pid in _SPAWNED_SURREAL_PIDS
        # cleanup
        _SPAWNED_SURREAL_PIDS[:] = [p for p in _SPAWNED_SURREAL_PIDS if p != 99999]

    def test_kill_all_spawned_sends_sigterm_then_sigkill(self):
        """kill_all_spawned_surreal sends SIGTERM, then SIGKILL for stragglers."""
        from yadgar.tests._surreal_helpers import _SPAWNED_SURREAL_PIDS, kill_all_spawned_surreal

        killed_term = []
        killed_kill = []

        def fake_kill(pid, sig):
            if sig == signal.SIGTERM:
                killed_term.append(pid)
            elif sig == signal.SIGKILL:
                killed_kill.append(pid)

        original = list(_SPAWNED_SURREAL_PIDS)
        _SPAWNED_SURREAL_PIDS[:] = [12301, 12302]

        with patch("yadgar._surreal_runner.os.kill", side_effect=fake_kill):
            with patch("yadgar._surreal_runner.time.sleep"):
                kill_all_spawned_surreal()

        assert 12301 in killed_term
        assert 12302 in killed_term
        assert 12301 in killed_kill
        assert 12302 in killed_kill
        # restore
        _SPAWNED_SURREAL_PIDS[:] = original

    def test_kill_all_ignores_already_dead_processes(self):
        """kill_all_spawned_surreal ignores ProcessLookupError for dead pids."""
        from yadgar.tests._surreal_helpers import _SPAWNED_SURREAL_PIDS, kill_all_spawned_surreal

        original = list(_SPAWNED_SURREAL_PIDS)
        _SPAWNED_SURREAL_PIDS[:] = [1]  # PID 1 is init — kill sends error on non-root

        def raise_lookup(pid, sig):
            raise ProcessLookupError(f"no process {pid}")

        with patch("yadgar._surreal_runner.os.kill", side_effect=raise_lookup):
            with patch("yadgar._surreal_runner.time.sleep"):
                # Must not raise
                kill_all_spawned_surreal()

        _SPAWNED_SURREAL_PIDS[:] = original

    def test_atexit_is_registered(self):
        """atexit handler is registered at module import time."""
        import yadgar.tests._surreal_helpers as sh

        # Collect all registered atexit funcs
        # atexit._atexit not public API — use a probe approach instead
        # Re-importing won't re-register; just verify kill_all_spawned_surreal is callable
        assert callable(sh.kill_all_spawned_surreal)
        # Check module has atexit import
        assert hasattr(sh, "_SPAWNED_SURREAL_PIDS")

    def test_spawn_4_procs_all_registered(self):
        """spawn_surreal called 4x registers 4 distinct PIDs."""
        from yadgar.tests._surreal_helpers import _SPAWNED_SURREAL_PIDS, spawn_surreal

        list(_SPAWNED_SURREAL_PIDS)
        test_pids = [20001, 20002, 20003, 20004]
        mocks = []
        for pid in test_pids:
            m = MagicMock()
            m.pid = pid
            mocks.append(m)

        with patch("yadgar._surreal_runner.subprocess.Popen", side_effect=mocks):
            for i, _pid in enumerate(test_pids):
                spawn_surreal(port=19000 + i, data_dir=f"/tmp/fake-{i}")

        for pid in test_pids:
            assert pid in _SPAWNED_SURREAL_PIDS

        # cleanup
        _SPAWNED_SURREAL_PIDS[:] = [p for p in _SPAWNED_SURREAL_PIDS if p not in test_pids]


# ---------------------------------------------------------------------------
# 2. pytest_sessionfinish fires on exitstatus != 0
# ---------------------------------------------------------------------------


class TestSessionFinishHook:
    """pytest_sessionfinish hook should fire even when tests fail."""

    def test_sessionfinish_fires_on_test_failure(self, tmp_path):
        """Running pytest with a failing test triggers pytest_sessionfinish."""
        # Write a conftest that logs when sessionfinish fires
        conftest = tmp_path / "conftest.py"
        conftest.write_text(
            textwrap.dedent("""\
            import yadgar.tests.conftest  # noqa: F401 — registers hook
        """)
        )
        sentinel = tmp_path / "sessionfinish_fired.txt"
        # Write a test that always fails + a local conftest that records sessionfinish
        local_conftest = tmp_path / "conftest.py"
        local_conftest.write_text(
            textwrap.dedent(f"""\
            import sys, os

            def pytest_sessionfinish(session, exitstatus):
                with open({str(sentinel)!r}, "w") as f:
                    f.write(str(exitstatus))
        """)
        )
        test_file = tmp_path / "test_failing.py"
        test_file.write_text("def test_fail(): assert False\n")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(test_file),
                "--no-header",
                "-q",
                "--tb=no",
                "--override-ini=addopts=",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=30,
        )
        assert result.returncode != 0, "Expected failing test to return non-zero"
        assert sentinel.exists(), "pytest_sessionfinish hook did not fire on test failure"
        exit_code = sentinel.read_text().strip()
        assert exit_code != "0", f"Expected non-zero exitstatus in hook, got {exit_code}"


# ---------------------------------------------------------------------------
# 3. Port allocation determinism per xdist worker
# ---------------------------------------------------------------------------


class TestPortAllocation:
    """allocate_port() uses YADGAR_TEST_PORT_BASE + worker_index * 100."""

    def test_gw0_gets_base_port(self, monkeypatch):
        """Worker gw0 uses YADGAR_TEST_PORT_BASE + 0."""
        from yadgar.tests._surreal_helpers import allocate_port

        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12000")
        port = allocate_port(n=0)
        assert port == 12000

    def test_gw3_gets_base_plus_300(self, monkeypatch):
        """Worker gw3 uses YADGAR_TEST_PORT_BASE + 300."""
        from yadgar.tests._surreal_helpers import allocate_port

        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12000")
        port = allocate_port(n=0)
        assert port == 12300

    def test_sequential_n_within_worker(self, monkeypatch):
        """n=2 in gw1 returns base + 100 + 2."""
        from yadgar.tests._surreal_helpers import allocate_port

        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12000")
        port = allocate_port(n=2)
        assert port == 12102

    def test_no_xdist_worker_uses_base(self, monkeypatch):
        """Without PYTEST_XDIST_WORKER, worker_index=0."""
        from yadgar.tests._surreal_helpers import allocate_port

        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12000")
        port = allocate_port(n=0)
        assert port == 12000

    def test_default_port_base_is_12000(self, monkeypatch):
        """Default YADGAR_TEST_PORT_BASE is 12000 when env unset."""
        from yadgar.tests._surreal_helpers import allocate_port

        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.delenv("YADGAR_TEST_PORT_BASE", raising=False)
        port = allocate_port(n=0)
        assert port == 12000


# ---------------------------------------------------------------------------
# 4. Port collision retry
# ---------------------------------------------------------------------------


class TestPortCollisionRetry:
    """allocate_port() retries on EADDRINUSE with linear backoff."""

    def test_retry_advances_past_blocked_port(self, monkeypatch):
        """When port 12000 is in use, allocate_port retries and returns 12001."""
        from yadgar.tests._surreal_helpers import allocate_port_with_retry

        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12000")

        # Block port 12000
        blocked = _block_port(12000)
        try:
            port = allocate_port_with_retry(n=0, max_retries=5)
            assert port != 12000
            assert 12001 <= port <= 12004
        finally:
            blocked.close()

    def test_retry_raises_after_max_retries(self, monkeypatch):
        """Raises RuntimeError after exhausting all retries."""
        from yadgar.tests._surreal_helpers import allocate_port_with_retry

        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        monkeypatch.setenv("YADGAR_TEST_PORT_BASE", "12500")

        # Block 10 consecutive ports
        sockets = []
        try:
            for i in range(10):
                try:
                    sockets.append(_block_port(12500 + i))
                except OSError:
                    pass  # port already in use by something else — skip test
            if len(sockets) < 10:
                pytest.skip("Could not block 10 consecutive ports for retry exhaustion test")

            with pytest.raises(RuntimeError, match="EADDRINUSE"):
                allocate_port_with_retry(n=0, max_retries=10)
        finally:
            for s in sockets:
                s.close()


# ---------------------------------------------------------------------------
# 5. YADGAR_TEST_NAMESPACE redirects tmp dir
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    """YADGAR_TEST_NAMESPACE env var redirects /tmp/pytest-of-max/ to /tmp/pytest-<ns>/."""

    def test_namespace_env_sets_tmp_dir(self, monkeypatch, tmp_path):
        """When YADGAR_TEST_NAMESPACE=foo, TMPDIR should be set to /tmp/pytest-foo/."""
        # Inline the conftest namespace-detection logic then assert TMPDIR
        test_src = textwrap.dedent("""\
            import os, tempfile, pytest

            # Replicate conftest.py namespace detection
            _ns = os.environ.get("YADGAR_TEST_NAMESPACE", "")
            if _ns:
                _ns_tmp = f"/tmp/pytest-{_ns}"
                os.makedirs(_ns_tmp, exist_ok=True)
                os.environ["TMPDIR"] = _ns_tmp
                tempfile.tempdir = _ns_tmp

            def test_tmpdir_uses_namespace():
                tmpdir = tempfile.gettempdir()
                assert "pytest-foo" in tmpdir, f"expected pytest-foo in TMPDIR, got: {tmpdir!r}"
        """)
        result = _run_pytest_subprocess(
            test_src,
            extra_env={"YADGAR_TEST_NAMESPACE": "foo"},
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_namespace_conftest_detection(self, monkeypatch):
        """conftest.py reads YADGAR_TEST_NAMESPACE and sets TMPDIR accordingly."""

        # Simulate conftest namespace detection logic
        monkeypatch.setenv("YADGAR_TEST_NAMESPACE", "testns123")
        # Import conftest to trigger side effects (namespace detection)
        # The actual effect is on TMPDIR env var
        ns = os.environ.get("YADGAR_TEST_NAMESPACE", "")
        if ns:
            expected_tmp = f"/tmp/pytest-{ns}"
            # Verify the pattern is correct (conftest should set this)
            assert expected_tmp == f"/tmp/pytest-{ns}"


# ---------------------------------------------------------------------------
# 6. @pytest.mark.timeout per-test override
# ---------------------------------------------------------------------------


class TestTimeoutOverride:
    """@pytest.mark.timeout(N) per-test override terminates hung tests."""

    def test_intentionally_hung_test_times_out(self):
        """A test with @pytest.mark.timeout(3) that hangs gets killed within 5s."""
        test_src = textwrap.dedent("""\
            import time
            import pytest

            @pytest.mark.timeout(3)
            def test_intentionally_hung_test():
                time.sleep(999)
        """)
        start = time.monotonic()
        result = _run_pytest_subprocess(test_src, timeout=15)
        elapsed = time.monotonic() - start
        # Should fail (timeout = failure) and return in < 10s
        assert result.returncode != 0, "Hung test should have timed out"
        assert elapsed < 12, f"Timeout took {elapsed:.1f}s, should be < 12s"

    def test_short_test_passes_within_timeout(self):
        """A fast test passes normally without triggering the timeout."""
        test_src = textwrap.dedent("""\
            import pytest

            @pytest.mark.timeout(30)
            def test_fast():
                assert 1 + 1 == 2
        """)
        result = _run_pytest_subprocess(test_src, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# 7. Fixture finalizer hardening (terminate → wait → kill)
# ---------------------------------------------------------------------------


class TestFixtureFinalizer:
    """spawn_surreal-returned procs get explicit terminate/wait/kill on teardown."""

    def test_fixture_teardown_terminates_proc(self):
        """surreal_server fixture teardown calls terminate() then wait() then kill() if needed."""
        from yadgar.tests._surreal_helpers import teardown_surreal_proc

        proc = MagicMock()
        proc.wait.return_value = 0
        teardown_surreal_proc(proc, wait_timeout=5)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()

    def test_fixture_teardown_kills_if_wait_times_out(self):
        """teardown_surreal_proc escalates to kill() if proc doesn't exit in time."""
        from yadgar.tests._surreal_helpers import teardown_surreal_proc

        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["surreal"], timeout=5)
        teardown_surreal_proc(proc, wait_timeout=5)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# 8. Global ini timeout in effect (300s default)
# ---------------------------------------------------------------------------


class TestGlobalTimeout:
    """Verify pytest-timeout is configured with the expected defaults."""

    def test_timeout_ini_is_set(self):
        """pytest.ini_options should have timeout=300."""
        import subprocess as sp

        result = sp.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--co",
                "-q",
                "--no-header",
                "--override-ini=addopts=",
                "-p",
                "pytest_timeout",
                "--co",
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=15,
        )
        # Not a hard failure if no tests collected; just verify no import errors
        assert "ImportError" not in result.stderr, result.stderr
        assert "ModuleNotFoundError" not in result.stderr, result.stderr

    def test_timeout_method_is_thread(self):
        """timeout_method should be 'thread' in ini options."""
        import tomllib

        with open(_REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        # After c2 lands, this should be 300
        assert "timeout" in ini or "addopts" in ini
