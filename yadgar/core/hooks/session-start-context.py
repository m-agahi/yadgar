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

try:
    from yadgar._shared.observability.observe import observe
    from yadgar._shared.observability.tracing import shutdown_tracing
except ImportError:

    def observe(*_a, **_k):
        return lambda fn: fn

    def shutdown_tracing(*_a, **_k):
        pass


try:
    from yadgar.core.hooks._identity_mint import resolve_session_project
except ImportError:  # yadgar not importable from this interpreter
    resolve_session_project = None


@observe(tier="stage")
def _close_http_error(http_exc) -> None:
    """Close a urllib HTTPError's file wrapper; never re-raise (py3.14 leak guard)."""
    try:
        http_exc.close()
    except Exception:  # noqa: BLE001 — close must never re-raise
        pass


@observe(tier="stage")
def _emit_project_id(cwd):
    """Mint the session's project_id, print the banner, return the id (or None).

    Car C2 / ADR-0227: this is the ONLY place a project identity is produced.
    Core and backend derive nothing, so the value has to be minted here, shown
    to the agent (it must pass ``project=`` explicitly — MCP calls carry no
    session key), and forwarded to the daemon as a query parameter.

    Fail-loud is about the IDENTITY, not the session: an unresolvable tree
    prints a loud notice with no candidate key and returns None, and any
    unexpected crash in the mint degrades to None rather than taking session
    start down with it.
    """
    if resolve_session_project is None:
        return None
    try:
        project_id, notice = resolve_session_project(cwd)
    except Exception:  # noqa: BLE001 — a mint crash must not brick SessionStart
        return None
    if notice:
        print(notice)
    return project_id


@observe(tier="boundary")
def main():
    try:
        # Read hook input from stdin to get cwd
        try:
            data = json.load(sys.stdin)
            cwd = data.get("cwd", os.getcwd())
        except Exception:
            cwd = os.getcwd()

        # Car C2: mint + emit BEFORE the daemon call — the identity line is the
        # transport, and the minted value is a query param on the call below.
        _project_id = _emit_project_id(cwd)

        # HTTP endpoint — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import contextlib as _contextlib
            import urllib.error as _err
            import urllib.parse as _parse
            import urllib.request as _req

            _params = {"directory": cwd}
            if _project_id:
                _params["project"] = _project_id
            _url = f"http://127.0.0.1:{_port}/hooks/session-context?{_parse.urlencode(_params)}"
            _token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
            _req_obj = _req.Request(_url)
            if _token:
                _req_obj.add_header("Authorization", f"Bearer {_token}")
            with _contextlib.closing(_req.urlopen(_req_obj, timeout=2)) as _resp:
                _text = json.loads(_resp.read().decode()).get("text", "")
            if _text:
                print(_text)
        except _err.HTTPError as _http_exc:
            # A non-200 response IS an HTTPError holding a file wrapper (a
            # tempfile._TemporaryFileWrapper via addbase on py3.14). Unclosed, its
            # deallocator fires a spurious ResourceWarning at a later GC that
            # pytest-xdist mis-attributes to an unrelated test. Close it here (via a
            # module helper to keep main() under the I13 nesting cap), then degrade
            # silently (same daemon-down contract as the broad except below).
            _close_http_error(_http_exc)
        except Exception:
            pass  # Daemon down — skip; never use surrealkv directly from host
    finally:
        shutdown_tracing()


if __name__ == "__main__":
    main()
