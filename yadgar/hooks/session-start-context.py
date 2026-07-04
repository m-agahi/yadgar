#!/usr/bin/env python3
"""Yadgar session context — SessionStart hook handler.

Injects recent project context into Claude's conversation on every
session start. Uses lightweight DB queries only — no ML model loading.

Output goes to stdout and is injected into Claude's context window.
HTTP-only: reads via daemon HTTP endpoint. No direct surrealkv access.
"""

import json
import os
import subprocess
import sys

try:
    from yadgar.observability.observe import observe
    from yadgar.tracing import shutdown_tracing
except ImportError:

    def observe(*_a, **_k):
        return lambda fn: fn

    def shutdown_tracing(*_a, **_k):
        pass


@observe(tier="boundary")
def main():
    try:
        # Read hook input from stdin to get cwd
        try:
            data = json.load(sys.stdin)
            cwd = data.get("cwd", os.getcwd())
        except Exception:
            cwd = os.getcwd()

        # v5.1.9 F1: capture branch on the HOST before calling the daemon.
        # The container cannot see host .git; the hook runs on the host and can.
        try:
            _r = subprocess.run(
                ["git", "-C", cwd, "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            _branch = _r.stdout.strip() if _r.returncode == 0 else ""
        except Exception:
            _branch = ""

        # HTTP endpoint — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import urllib.parse as _parse
            import urllib.request as _req

            _params = {"directory": cwd}
            if _branch:
                _params["branch"] = _branch
            _url = f"http://127.0.0.1:{_port}/hooks/session-context?{_parse.urlencode(_params)}"
            _token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
            _req_obj = _req.Request(_url)
            if _token:
                _req_obj.add_header("Authorization", f"Bearer {_token}")
            _resp = _req.urlopen(_req_obj, timeout=2)
            _text = json.loads(_resp.read().decode()).get("text", "")
            if _text:
                print(_text)
        except Exception:
            pass  # Daemon down — skip; never use surrealkv directly from host
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
