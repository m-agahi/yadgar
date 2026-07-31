"""version — print yadgar core + backend + daemon-probe summary."""

import json
import sys
import urllib.error
import urllib.request

import yadgar._shared.paths as _paths


def _read_auth_token() -> str | None:
    """Read YADGAR_MCP_AUTH_TOKEN from env or ~/.config/yadgar/secrets.env."""
    import os

    token = os.environ.get("YADGAR_MCP_AUTH_TOKEN")
    if token:
        return token
    secrets_path = _paths.SECRETS_ENV_PATH
    if secrets_path.exists():
        try:
            for line in secrets_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("YADGAR_MCP_AUTH_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None


def _probe_daemon() -> dict:
    """Probe http://localhost:8765/health.  Returns dict with 'running' key."""
    url = "http://localhost:8765/health"
    token = _read_auth_token()
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "running": True,
            "version": data.get("version", "unknown"),
            "uptime_seconds": data.get("uptime_seconds"),
            "db": data.get("db") in (True, "ok"),
            "embed": data.get("embed") in (True, "ok"),
        }
    except urllib.error.HTTPError as e:
        # Close the file wrapper (py3.14 ResourceWarning leak guard).
        e.close()
        return {"running": False}
    except Exception:
        return {"running": False}


def print_version_summary(json_mode: bool = False) -> None:
    """Print yadgar core + backend + daemon version summary to stdout."""
    from yadgar import BACKEND_VERSION, __version__

    daemon = _probe_daemon()

    if json_mode:
        payload = {
            "core": __version__,
            "backend": BACKEND_VERSION,
            "daemon": daemon,
        }
        print(json.dumps(payload, indent=2))
        return

    # Text mode
    label_w = 10  # "backend" is 7 chars; pad to 10 for alignment
    print(f"yadgar {'core':<{label_w}} {__version__}")
    print(f"yadgar {'backend':<{label_w}} {BACKEND_VERSION}")

    if daemon["running"]:
        ver = daemon.get("version", "unknown")
        uptime = daemon.get("uptime_seconds")
        db_ok = "ok" if daemon.get("db") else "error"
        embed_ok = "ok" if daemon.get("embed") else "error"
        uptime_str = f"uptime {uptime}s, " if uptime is not None else ""
        print(f"yadgar {'daemon':<{label_w}} {ver} ({uptime_str}db {db_ok}, embed {embed_ok})")
    else:
        print(
            f"yadgar {'daemon':<{label_w}} not running"
            " (start with `systemctl --user start yadgar.target`)"
        )


if __name__ == "__main__":  # pragma: no cover
    json_mode = "--json" in sys.argv or "--format=json" in sys.argv
    print_version_summary(json_mode=json_mode)
