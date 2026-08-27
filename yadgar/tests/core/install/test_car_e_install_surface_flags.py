"""Task 399 — ``yadgar install`` surface-flag selection.

Prior bug: ``cmd_install`` ended with

    if not want_mcp and not want_rules:
        want_mcp = want_rules = True

so ``yadgar install --client claude-code --hooks`` — naming ONLY the hooks
surface — silently switched the MCP and rules surfaces on too and rewrote the
client's MCP config (``~/.claude.json``: nix writes ``"type": "http"`` with a
literal bearer token, the installer writes ``"type": "streamable-http"``).  The
``--hooks`` help text claimed the flag "explicitly opts in … kept for symmetry
with --mcp / --rules", which was false — it opted in to everything.

These tests pin the fixed contract:
  * the MCP + rules default fires only when NO surface flag is named,
  * ``--hooks`` alone touches the hooks surface ONLY,
  * ``--no-hooks`` is not a surface name (it keeps the MCP + rules default),
  * ``--hooks --no-hooks`` is rejected rather than becoming a silent no-op,
  * the help text no longer lies.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from yadgar.core.cli.install import cmd_install, register


def _args(**overrides) -> SimpleNamespace:
    base: dict[str, object] = {
        "client": "claude-code",
        "auto_detect": False,
        "mcp": False,
        "rules": False,
        "hooks": False,
        "no_hooks": False,
        "print": False,
        "port": 8765,
        "scope": "global",
        "project_directory": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def captured_opts(monkeypatch):
    """Capture the InstallOptions cmd_install hands to install_client.

    Patches the orchestrator so no file is ever written, whatever the flags.
    """
    seen: list = []

    import yadgar.core.install.clients.install as inst

    def _fake_install_client(name, opts=None, **kwargs):
        seen.append(opts)
        return {"client": name, "mcp": None, "rules": None, "hooks": None, "dry_run": False}

    monkeypatch.setattr(inst, "install_client", _fake_install_client)
    return seen


# ── The seven surface-flag combinations ────────────────────────────────────

# (mcp, rules, hooks) flags  →  (want_mcp, want_rules, want_hooks)
_MATRIX = [
    # No surface flag named → the MCP + rules default, hooks default-on.
    ((False, False, False), (True, True, True)),
    ((True, False, False), (True, False, True)),
    ((False, True, False), (False, True, True)),
    # The regression this task fixes: hooks ONLY.
    ((False, False, True), (False, False, True)),
    ((True, True, False), (True, True, True)),
    ((True, False, True), (True, False, True)),
    ((False, True, True), (False, True, True)),
    ((True, True, True), (True, True, True)),
]


@pytest.mark.parametrize(("flags", "expected"), _MATRIX)
def test_surface_flag_matrix(flags, expected, captured_opts):
    mcp, rules, hooks = flags
    cmd_install(_args(mcp=mcp, rules=rules, hooks=hooks))
    assert len(captured_opts) == 1
    opts = captured_opts[0]
    assert (opts.mcp, opts.rules, opts.hooks) == expected, (
        f"--mcp={mcp} --rules={rules} --hooks={hooks} selected "
        f"{(opts.mcp, opts.rules, opts.hooks)}, expected {expected}"
    )


def test_hooks_alone_does_not_touch_mcp_or_rules(captured_opts):
    """The headline regression: ``--hooks`` must not drag MCP + rules along."""
    cmd_install(_args(hooks=True))
    opts = captured_opts[0]
    assert opts.hooks is True
    assert opts.mcp is False, "--hooks alone must NOT install the MCP surface"
    assert opts.rules is False, "--hooks alone must NOT install the rules surface"


def test_no_hooks_is_not_a_surface_name(captured_opts):
    """``--no-hooks`` deselects hooks and keeps the MCP + rules default."""
    cmd_install(_args(no_hooks=True))
    opts = captured_opts[0]
    assert (opts.mcp, opts.rules, opts.hooks) == (True, True, False)


def test_no_hooks_with_mcp_selects_mcp_only(captured_opts):
    cmd_install(_args(mcp=True, no_hooks=True))
    opts = captured_opts[0]
    assert (opts.mcp, opts.rules, opts.hooks) == (True, False, False)


def test_auto_detect_honours_hooks_only(monkeypatch):
    """The --auto-detect branch reads the same want_* values."""
    seen: list = []

    import yadgar.core.install.clients.install as inst

    def _fake_auto(opts=None, **kwargs):
        seen.append(opts)
        return []

    monkeypatch.setattr(inst, "install_auto_detect", _fake_auto)
    cmd_install(_args(client=None, auto_detect=True, hooks=True))
    assert (seen[0].mcp, seen[0].rules, seen[0].hooks) == (False, False, True)


# ── --print: the surfaces actually rendered ────────────────────────────────


def test_print_hooks_only_renders_no_mcp_or_rules_fragment(capsys, tmp_path, monkeypatch):
    """End-to-end through the real orchestrator in dry-run: ``--hooks --print``
    emits a hooks fragment and NULL mcp/rules fragments (no file written)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd_install(_args(hooks=True, **{"print": True}))
    out = capsys.readouterr().out
    # The hooks emitter prints its own "[dry-run] Would write to:" preview
    # ahead of cmd_install's result document; the result is the LAST
    # column-0 JSON object on stdout.
    start = out.rindex("\n{\n") + 1 if "\n{\n" in out else out.index("{")
    payload = json.loads(out[start:])
    assert payload["mcp"] is None
    assert payload["rules"] is None
    assert payload["hooks"] is not None
    assert payload["dry_run"] is True
    assert not (tmp_path / ".claude.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


# ── Parser-level contract ──────────────────────────────────────────────────


def _parsers() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """Return (root parser, the ``install`` subparser)."""
    root = argparse.ArgumentParser()
    subs = root.add_subparsers()
    register(subs)
    return root, subs.choices["install"]


def _parser() -> argparse.ArgumentParser:
    return _parsers()[0]


def test_hooks_and_no_hooks_are_mutually_exclusive():
    """Both flags together would select NOTHING — argparse must reject the
    pair rather than let the command become a silent no-op."""
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(["install", "--client", "claude-code", "--hooks", "--no-hooks"])
    assert exc.value.code == 2


def test_hooks_flag_parses_alone():
    args = _parser().parse_args(["install", "--client", "claude-code", "--hooks"])
    assert args.hooks is True
    assert args.no_hooks is False
    assert args.mcp is False
    assert args.rules is False


def test_hooks_help_text_does_not_claim_symmetry_noop():
    """The old help said --hooks "explicitly opts in … kept for symmetry with
    --mcp / --rules" — describing a no-op. It is now a real surface selector."""
    help_text = _parsers()[1].format_help()
    assert "symmetry" not in help_text
    assert "hooks surface ONLY" in help_text
