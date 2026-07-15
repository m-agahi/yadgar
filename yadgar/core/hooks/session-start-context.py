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
    """Car 0 §0.1: compute the two TRUSTED per-directory git facts HOST-SIDE.

    The container cannot see the host ``.git``; this hook runs on the host and can.
    Returns ``(gitness: bool, default_branch: str)`` — ``default_branch`` is ""
    (→ NULL server-side) when non-git or git errors.
      gitness        = `git rev-parse --is-inside-work-tree` prints "true".
      default_branch = last segment of `symbolic-ref refs/remotes/origin/HEAD`.
    """
    try:
        _gr = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        gitness = _gr.returncode == 0 and _gr.stdout.strip() == "true"
    except Exception:
        return False, ""

    if not gitness:
        return False, ""

    default_branch = ""
    try:
        _dr = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if _dr.returncode == 0:
            _out = _dr.stdout.strip()
            # e.g. "refs/remotes/origin/master" → "master"
            default_branch = _out.rsplit("/", 1)[-1] if _out else ""
    except Exception:
        default_branch = ""
    return True, default_branch


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

        # Car 0 §0.1: the two TRUSTED per-directory git facts (host-side). This
        # SessionStart endpoint is the SOLE set-channel for gitness/default_branch
        # — no model tool writes them, so the canonical decision is non-forgeable.
        _gitness, _default_branch = _compute_git_facts(cwd)

        # HTTP endpoint — works in daemon mode where DB lock is always held
        _port = os.environ.get("YADGAR_PORT", "8765")
        try:
            import urllib.parse as _parse
            import urllib.request as _req

            _params = {"directory": cwd}
            if _branch:
                _params["branch"] = _branch
            # Car 0: TRUSTED git facts — gitness always sent (always meaningful);
            # default_branch omitted when empty (non-git → NULL server-side).
            _params["gitness"] = "true" if _gitness else "false"
            if _default_branch:
                _params["default_branch"] = _default_branch
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
