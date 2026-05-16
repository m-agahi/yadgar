#!/usr/bin/env python3
"""Yadgar session context — SessionStart hook handler.

Injects recent project context into Claude's conversation on every
session start. Uses lightweight DB queries only — no ML model loading.

Output goes to stdout and is injected into Claude's context window.
HTTP-only: reads via daemon HTTP endpoint. No direct surrealkv access.
"""

import json
import os
import sys


def main():
    # Read hook input from stdin to get cwd
    try:
        data = json.load(sys.stdin)
        cwd = data.get("cwd", os.getcwd())
    except Exception:
        cwd = os.getcwd()

    # HTTP endpoint — works in daemon mode where DB lock is always held
    _port = os.environ.get("YADGAR_PORT", "8765")
    try:
        import urllib.parse as _parse
        import urllib.request as _req

        _url = f"http://127.0.0.1:{_port}/hooks/session-context?directory={_parse.quote(cwd)}"
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


if __name__ == "__main__":
    main()
