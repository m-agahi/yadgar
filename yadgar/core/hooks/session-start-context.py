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

# Car 9: route through the ONE sanctioned bearer-token resolver (env var,
# else secrets.env) rather than a bare os.environ.get — falls back to the
# bare env read if yadgar (or its lazy _shared.paths dependency) is not
# importable from this interpreter, mirroring resolve_session_project above.
try:
    from yadgar.core.install.auth_token import resolve_auth_token
except ImportError:  # yadgar not importable from this interpreter
    resolve_auth_token = None


@observe(tier="stage")
def _close_http_error(http_exc) -> None:
    """Close a urllib HTTPError's file wrapper; never re-raise (py3.14 leak guard)."""
    try:
        http_exc.close()
    except Exception:  # noqa: BLE001 — close must never re-raise
        pass


@observe(tier="stage")
def _seed_task_list(session_id, tasks):
    """Write the ledger's open tasks into the harness task store. True on success.

    Car C. Returns False when the seeder is unavailable, guards off, or trips —
    the caller then prints the fallback nudge instead. A partial seed still
    returns True: a short list beats a list the model duplicates by hand.
    """
    if not session_id or not tasks:
        return False
    try:
        from yadgar.core.hooks.task_seed import seed_harness_task_list
    except ImportError:  # yadgar not importable from this interpreter
        return False
    try:
        return bool(seed_harness_task_list(session_id, tasks).get("ok"))
    except Exception:  # noqa: BLE001 — seeding must never brick SessionStart
        return False


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
    # Publish cwd -> project_id so a client using ONE global ``mcpServers``
    # entry can still resolve identity: such sessions are indistinguishable on
    # the wire, so the daemon looks the caller's ``directory`` up in this
    # table. Only this host-side process can mint truthfully (ADR-0227), so
    # only it can write the table. Best-effort — never brick SessionStart.
    if project_id:
        try:
            from yadgar._shared.runtime.session_map import register_session_project

            register_session_project(cwd, project_id)
        except Exception:  # noqa: BLE001
            pass
    return project_id


@observe(tier="boundary")
def main():
    try:
        # Read hook input from stdin to get cwd
        try:
            data = json.load(sys.stdin)
            cwd = data.get("cwd", os.getcwd())
            # Car C: names the harness task store to seed (~/.claude/tasks/<id>).
            session_id = data.get("session_id") or ""
        except Exception:
            cwd = os.getcwd()
            session_id = ""

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
            if session_id:
                # Car C capability flag: this hook can seed the harness task
                # store on disk, so the daemon hands back the open task rows
                # and keeps the (expensive) hand-mirror nudge out of the render.
                _params["seed"] = "1"
            _url = f"http://127.0.0.1:{_port}/hooks/session-context?{_parse.urlencode(_params)}"
            _token = (
                resolve_auth_token()
                if resolve_auth_token is not None
                else os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
            )
            _req_obj = _req.Request(_url)
            if _token:
                _req_obj.add_header("Authorization", f"Bearer {_token}")
            with _contextlib.closing(_req.urlopen(_req_obj, timeout=2)) as _resp:
                _payload = json.loads(_resp.read().decode())
            _text = _payload.get("text", "")
            # Car C: seed mechanically; the nudge is the fallback for a tripped
            # guard only. Both keys are absent when the daemon predates Car C,
            # in which case the nudge is already inside _text as before.
            _nudge = _payload.get("task_nudge", "")
            if _nudge:
                _tasks = _payload.get("tasks") or []
                if _seed_task_list(session_id, _tasks):
                    print(
                        f"[yadgar] Task list seeded from the ledger "
                        f"({len(_tasks)} open task(s)) — already in TaskList, do not re-create."
                    )
                else:
                    print(_nudge)
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
