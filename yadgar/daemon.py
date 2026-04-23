"""Yadgar daemon management — start/stop/status for persistent server mode.

In daemon mode Yadgar runs as a single background process holding the
surrealkv DB lock. All Claude sessions connect via HTTP instead of each
spawning their own process. This eliminates the lock-contention problem
that prevents multiple sessions from having working Yadgar tools.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8765
_PID_FILE = Path("~/.yadgar/yadgar.pid")
_HEALTH_TIMEOUT = 10.0  # seconds to wait for server to become healthy after start


class YadgarDaemon:
    def __init__(self, port: int = DEFAULT_PORT, db_path: str | None = None):
        self.port = port
        self.db_path = db_path
        self._pid_file = _PID_FILE.expanduser()

    # ── public API ─────────────────────────────────────────────────────────

    def start(self) -> dict:
        """Start the daemon. No-op if already running."""
        if self._is_running():
            return {"status": "already_running", "pid": self._read_pid(), "port": self.port}

        # Check DB lock before spawning — another process (per-session MCP) may hold it
        if self._db_locked():
            return {
                "status": "failed",
                "reason": (
                    "DB lock is held by another Yadgar process. "
                    "Close all Claude sessions using Yadgar, then start the daemon."
                ),
            }

        self._pid_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            "-m",
            "yadgar",
            "--transport",
            "streamable-http",
            "--port",
            str(self.port),
            "--quiet",
        ]
        if self.db_path:
            cmd += ["--db-path", self.db_path]

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,  # detach from terminal/session
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        self._pid_file.write_text(str(proc.pid))

        # Poll health endpoint until ready
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not self._pid_alive(proc.pid):
                return {"status": "failed", "reason": "process exited immediately"}
            if self._health_ok():
                return {"status": "started", "pid": proc.pid, "port": self.port}

        return {
            "status": "started",
            "pid": proc.pid,
            "port": self.port,
            "warning": "health check timed out — server may still be loading",
        }

    def stop(self) -> dict:
        """Stop the daemon gracefully (SIGTERM, then SIGKILL after 10s)."""
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            # PID file missing or stale — scan /proc for a process holding the DB lock
            pid = self._find_pid_by_lock()
            if pid is None:
                self._pid_file.unlink(missing_ok=True)
                return {"status": "not_running"}

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._pid_file.unlink(missing_ok=True)
            return {"status": "not_running"}

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if not self._pid_alive(pid):
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self._pid_file.unlink(missing_ok=True)
        return {"status": "stopped", "pid": pid}

    def status(self) -> dict:
        """Return daemon status dict."""
        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            if pid is not None:
                self._pid_file.unlink(missing_ok=True)
            pid = self._find_pid_by_lock()
            if pid is None:
                return {"running": False}

        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2)
            health = json.loads(resp.read().decode())
            return {"running": True, "pid": pid, "port": self.port, **health}
        except Exception:
            return {"running": True, "pid": pid, "port": self.port, "health": "unreachable"}

    def restart(self) -> dict:
        stop_result = self.stop()
        start_result = self.start()
        return {"stopped": stop_result, "started": start_result}

    def configure_mcp(self) -> dict:
        """Switch ~/.claude.json MCP config to HTTP transport.

        After this, every Claude session will connect to the running daemon
        instead of spawning its own yadgar process.
        """
        config_path = Path.home() / ".claude.json"
        config: dict = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
            except Exception:
                config = {}

        old = config.get("mcpServers", {}).get("yadgar", {})
        mcp_servers = config.get("mcpServers", {})
        mcp_servers["yadgar"] = {
            "type": "sse",
            "url": f"http://127.0.0.1:{self.port}/sse",
        }
        config["mcpServers"] = mcp_servers
        config_path.write_text(json.dumps(config, indent=2))

        return {
            "updated": str(config_path),
            "old": old,
            "new": mcp_servers["yadgar"],
        }

    def install_systemd_service(self) -> dict:
        """Write a systemd user service unit for auto-start on login."""
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True, exist_ok=True)

        extra_args = f" --db-path {self.db_path}" if self.db_path else ""
        unit = f"""\
[Unit]
Description=Yadgar Memory Engine
After=network.target

[Service]
Type=simple
ExecStart={sys.executable} -m yadgar --transport streamable-http --port {self.port} --quiet{extra_args}
Restart=on-failure
RestartSec=5
Environment=YADGAR_PORT={self.port}

[Install]
WantedBy=default.target
"""
        service_path = service_dir / "yadgar.service"
        service_path.write_text(unit)

        return {
            "service_file": str(service_path),
            "enable": "systemctl --user enable yadgar",
            "start": "systemctl --user start yadgar",
            "status": "systemctl --user status yadgar",
        }

    # ── internals ──────────────────────────────────────────────────────────

    def _db_locked(self) -> bool:
        """Return True if another process holds the surrealkv DB lock."""
        import fcntl

        from yadgar.config import get_settings

        settings = get_settings()
        db_path = Path(self.db_path or settings.DB_PATH).expanduser()
        lock_path = db_path.parent / "yadgar.lock"
        if not lock_path.exists():
            return False
        try:
            lf = open(lock_path)
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
            return False
        except OSError:
            return True

    def _read_pid(self) -> int | None:
        if not self._pid_file.exists():
            return None
        try:
            return int(self._pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but we can't signal it

    def _is_running(self) -> bool:
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid):
            return True
        return self._find_pid_by_lock() is not None

    def _find_pid_by_lock(self) -> int | None:
        """Find a running yadgar process by scanning /proc for the DB lock file.

        Used as fallback when the PID file is missing (e.g. daemon started by
        systemd/home-manager rather than `yadgar daemon start`).
        """
        try:
            from yadgar.config import get_settings

            settings = get_settings()
            db_path = Path(self.db_path or settings.DB_PATH).expanduser()
            lock_path = db_path.parent / "yadgar.lock"
            if not lock_path.exists():
                return None
            lock_str = str(lock_path)
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    for fd_link in (pid_dir / "fd").iterdir():
                        try:
                            if os.readlink(str(fd_link)) == lock_str:
                                return int(pid_dir.name)
                        except (OSError, ValueError):
                            continue
                except (PermissionError, FileNotFoundError, OSError):
                    continue
        except Exception:
            pass
        return None

    def _health_ok(self) -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1)
            return True
        except Exception:
            return False
