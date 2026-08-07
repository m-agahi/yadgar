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
    from yadgar._shared.observability.observe import observe
    from yadgar._shared.observability.tracing import shutdown_tracing
except ImportError:

    def observe(*_a, **_k):
        return lambda fn: fn

    def shutdown_tracing(*_a, **_k):
        pass


def _compute_git_facts(cwd):
    """Compute the TRUSTED per-directory git fact HOST-SIDE.

    The container cannot see the host ``.git``; this hook runs on the host and can.
    Returns ``gitness: bool`` — `git rev-parse --is-inside-work-tree` prints "true".

    ADR-0216: this used to return ``(gitness, default_branch)``. Branch scoping was
    removed by ADR-0215, so only ``gitness`` survives.
    """
    try:
        _gr = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return _gr.returncode == 0 and _gr.stdout.strip() == "true"
    except Exception:
        return False


@observe(tier="stage")
def _close_http_error(http_exc) -> None:
    """Close a urllib HTTPError's file wrapper; never re-raise (py3.14 leak guard)."""
    try:
        http_exc.close()
    except Exception:  # noqa: BLE001 — close must never re-raise
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

        # The TRUSTED per-directory git fact (host-side). This SessionStart
        # endpoint is the SOLE set-channel for gitness — no model tool writes it,
        # so the fact stays non-forgeable (ADR-0126's surviving half).
        _gitness = _compute_git_facts(cwd)

        # HTTP endpoint — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import contextlib as _contextlib
            import urllib.error as _err
            import urllib.parse as _parse
            import urllib.request as _req

            # TRUSTED git fact — gitness is always sent (always meaningful).
            _params = {"directory": cwd, "gitness": "true" if _gitness else "false"}
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
