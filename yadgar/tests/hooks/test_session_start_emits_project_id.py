"""Car C2 of the 0047 PR-40 remediation train — the SessionStart emit.

ADR-0227 removes every container-side derivation, so the ONLY way a tool call
can carry a correct ``project`` is for the host-side SessionStart hook to mint
the value and put it where the agent will read it. Per §1.3 T1 of
``docs/plans/0047-pr40-remediation-2026-08-10.md`` there is no daemon-side
session registry and no per-session env var — the transport IS the emitted
line plus an explicit caller parameter.

Two surfaces are pinned here:

* ``yadgar/core/hooks/session-start-context.py`` — the Claude Code hook script.
* ``yadgar.core.cli.hook.hook_session_start_context`` /
  ``hook_post_compact_rehydrate`` — the same handlers for the CLI/opencode
  transport (the opencode plugin shells out to ``yadgar hook <event>``).

Both must (a) print the greppable ``yadgar: project_id=…`` banner, (b) forward
the minted value to the daemon as an explicit ``project`` query parameter, and
(c) on mint failure print a loud error and emit NO guessed value.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

_HOOK = Path(__file__).parent.parent.parent / "core" / "hooks" / "session-start-context.py"


def _load_hook():
    """Import the hook module from its file path, bypassing the __main__ guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_session_start_hook_c2", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(hook_mod, cwd: str):
    """Drive ``main()`` with a hook payload; the daemon is always unreachable."""
    old_stdin = hook_mod.sys.stdin
    hook_mod.sys.stdin = io.StringIO(json.dumps({"cwd": cwd}))
    try:
        with patch("urllib.request.urlopen", side_effect=OSError("daemon down")):
            hook_mod.main()
    finally:
        hook_mod.sys.stdin = old_stdin


# ── the Claude Code hook script ───────────────────────────────────────────


def test_session_start_hook_emits_the_project_id_banner(capsys):
    """A successful mint prints the machine-greppable identity line."""
    with patch(
        "yadgar.core.hooks._identity_mint.resolve_session_project",
        return_value=(
            "m-agahi/yadgar",
            'yadgar: project_id=m-agahi/yadgar — pass project="m-agahi/yadgar" on every yadgar tool call.',
        ),
    ):
        hook_mod = _load_hook()
        _run_hook(hook_mod, "/repo")

    out = capsys.readouterr().out
    assert "yadgar: project_id=m-agahi/yadgar" in out, (
        f"SessionStart must emit the identity line into the agent's context; got {out!r}"
    )


def test_session_start_hook_passes_project_to_the_daemon():
    """The minted value travels to /hooks/session-context as ``project=``.

    ADR-0227: core-server must not derive. The endpoint's project-scoped work
    (the task-ledger restore nudge) can only be correct if the hook hands it
    the minted key.
    """
    seen: dict[str, str] = {}

    def _capture(req, *a, **k):
        seen["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        raise OSError("daemon down")

    with patch(
        "yadgar.core.hooks._identity_mint.resolve_session_project",
        return_value=("m-agahi/yadgar", "yadgar: project_id=m-agahi/yadgar"),
    ):
        hook_mod = _load_hook()
        old_stdin = hook_mod.sys.stdin
        hook_mod.sys.stdin = io.StringIO(json.dumps({"cwd": "/repo"}))
        try:
            with patch("urllib.request.urlopen", side_effect=_capture):
                hook_mod.main()
        finally:
            hook_mod.sys.stdin = old_stdin

    assert "url" in seen, "the hook never called the daemon"
    assert "project=m-agahi%2Fyadgar" in seen["url"] or "project=m-agahi/yadgar" in seen["url"], (
        f"the minted project_id must be forwarded as a query param; got {seen['url']!r}"
    )


def test_session_start_hook_failure_emits_no_guessed_value(capsys):
    """A mint failure prints a loud error and NEVER a project_id line."""
    with patch(
        "yadgar.core.hooks._identity_mint.resolve_session_project",
        return_value=(None, "[yadgar] ERROR: cannot determine project_id for /repo."),
    ):
        hook_mod = _load_hook()
        _run_hook(hook_mod, "/repo")

    out = capsys.readouterr().out
    assert "ERROR" in out, f"mint failure must be loud; got {out!r}"
    assert "project_id=" not in out, (
        f"a failed mint must not emit a project_id an agent would copy; got {out!r}"
    )


def test_session_start_hook_survives_a_raising_mint(capsys):
    """A crash in the mint must never take down session start.

    Fail-loud is about identity, not about bricking the session: the hook still
    has to return so the rest of the context injection happens.
    """
    with patch(
        "yadgar.core.hooks._identity_mint.resolve_session_project",
        side_effect=RuntimeError("boom"),
    ):
        hook_mod = _load_hook()
        _run_hook(hook_mod, "/repo")  # must not raise

    out = capsys.readouterr().out
    assert "project_id=" not in out


# ── the CLI / opencode transport ──────────────────────────────────────────


@pytest.mark.parametrize(
    "handler_name",
    ["hook_session_start_context", "hook_post_compact_rehydrate"],
)
def test_cli_hook_handlers_emit_the_banner(handler_name, capsys, monkeypatch):
    """Both CLI SessionStart handlers mint and emit — not just the Claude one.

    ``hook_post_compact_rehydrate`` is a SessionStart(compact) handler: after a
    compaction the agent has lost the original banner, so it must be re-emitted
    or the identity is gone for the rest of the session.
    """
    from yadgar.core.cli import hook as cli_hook

    monkeypatch.setattr(cli_hook.sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo"})))
    with (
        patch(
            "yadgar.core.hooks._identity_mint.resolve_session_project",
            return_value=("m-agahi/yadgar", "yadgar: project_id=m-agahi/yadgar"),
        ),
        patch.object(cli_hook, "_http_get", return_value=None),
    ):
        getattr(cli_hook, handler_name)()

    out = capsys.readouterr().out
    assert "yadgar: project_id=m-agahi/yadgar" in out, (
        f"{handler_name} must emit the identity banner; got {out!r}"
    )


@pytest.mark.parametrize(
    ("handler_name", "endpoint"),
    [
        ("hook_session_start_context", "/hooks/session-context"),
        ("hook_post_compact_rehydrate", "/hooks/post-compact"),
    ],
)
def test_cli_hook_handlers_forward_project_param(handler_name, endpoint, monkeypatch):
    """Both CLI handlers pass ``project`` in the daemon query params."""
    from yadgar.core.cli import hook as cli_hook

    seen: dict = {}

    def _capture(path, params):
        seen["path"] = path
        seen["params"] = params
        return None

    monkeypatch.setattr(cli_hook.sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo"})))
    with (
        patch(
            "yadgar.core.hooks._identity_mint.resolve_session_project",
            return_value=("m-agahi/yadgar", "yadgar: project_id=m-agahi/yadgar"),
        ),
        patch.object(cli_hook, "_http_get", _capture),
    ):
        getattr(cli_hook, handler_name)()

    assert seen.get("path") == endpoint
    assert seen["params"].get("project") == "m-agahi/yadgar", (
        f"{handler_name} must forward the minted project_id; got {seen.get('params')!r}"
    )
