"""Ledger task #322 — execution probe for managed-hook verification.

The existing ``verify_managed_hooks`` answers REGISTERED (a logical name appears
in some settings.json).  Task #322 wants WORK — actually invoke each registered
runner-dispatched hook with a minimal valid stdin payload and classify the
outcome.  A hook that is registered but broken (binary missing, runtime crash,
hang) must flip ``ok`` to False so the user gets a real signal.

The probe runs ONLY on the yadgar-installer dispatch shape
(``hook_runner.py <name>``).  Nix's ``yadgar-<name>.py`` standalone shape is
skipped: probing it would double-count hooks with their runner-dispatched twin,
and the runner IS the canonical shape yadgar installs.

Tests monkeypatch ``subprocess.run`` and the expected-set harvest so they do NOT
execute real hook binaries or run the real installer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from yadgar.core.install._verify import (
    _probe_hook_execution,
    format_hook_verify_report,
    verify_managed_hooks,
)


def _entry(command: str, matcher: str = "") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


_VENVPY = "/home/user/.local/pipx/venvs/yadgar/bin/python"
_HOOKS = "/home/user/.claude/hooks"


def _runner_cmd(name: str) -> str:
    return f"{_VENVPY} {_HOOKS}/hook_runner.py {name}"


def _standalone_cmd(name: str) -> str:
    return f"{_VENVPY} {_HOOKS}/yadgar-{name}.py"


def _write_global_settings(home: Path, data: dict) -> Path:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    path = claude / "settings.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def _patched_subprocess(outcome: str, stderr: str = "", returncode: int = 0):
    """Return a fake ``subprocess.run`` factory.

    *outcome*:
      - ``"ok"`` → CompletedProcess(returncode=0, stderr="")
      - ``"crash"`` → CompletedProcess(returncode=1, stderr=<stderr>)
      - ``"hang"`` → raises subprocess.TimeoutExpired
      - ``"binary-missing"`` → raises FileNotFoundError
    """
    if outcome == "hang":

        def _hang_run(*args: Any, **kwargs: Any):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 0))

        return _hang_run
    if outcome == "binary-missing":

        def _missing_run(*args: Any, **kwargs: Any):
            raise FileNotFoundError("simulated: no such file")

        return _missing_run

    def _ok_run(*args: Any, **kwargs: Any):
        return subprocess.CompletedProcess(
            args=args[0], returncode=returncode, stdout="", stderr=stderr
        )

    return _ok_run


# ── probe unit-tests (no settings.json wiring, in-process only) ──────────────


def test_present_hook_returns_ran(monkeypatch):
    """A successful (returncode 0) hook → 'ran'."""
    cmd = _runner_cmd("post-tool-capture")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", _patched_subprocess("ok"))

    result = _probe_hook_execution(entries, timeout=2.0)
    assert result["post-tool-capture"]["status"] == "ran"


def test_hung_hook_returns_hung(monkeypatch):
    """A subprocess that exceeds the timeout → 'hung'."""
    cmd = _runner_cmd("pre-compact-drain")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", _patched_subprocess("hang"))

    result = _probe_hook_execution(entries, timeout=0.5)
    assert result["pre-compact-drain"]["status"] == "hung"


def test_binary_missing(monkeypatch):
    """FileNotFoundError on subprocess.run → 'binary-missing'."""
    cmd = _runner_cmd("session-start-context")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    monkeypatch.setattr(
        "yadgar.core.install._verify.subprocess.run", _patched_subprocess("binary-missing")
    )

    result = _probe_hook_execution(entries, timeout=2.0)
    assert result["session-start-context"]["status"] == "binary-missing"


def test_crash_hook_returns_crash_with_stderr(monkeypatch):
    """A non-zero exit with stderr text → 'crash' + first 200 chars of stderr."""
    cmd = _runner_cmd("block-reflect")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    long_stderr = "boom " * 200
    monkeypatch.setattr(
        "yadgar.core.install._verify.subprocess.run",
        _patched_subprocess("crash", stderr=long_stderr, returncode=1),
    )

    result = _probe_hook_execution(entries, timeout=2.0)
    rec = result["block-reflect"]
    assert rec["status"] == "crash"
    assert "boom" in rec["crash_reason"]
    assert len(rec["crash_reason"]) <= 200


def test_nix_standalone_shape_skipped_from_probe(monkeypatch):
    """A yadgar-<name>.py standalone script is NOT probed."""
    cmd = _standalone_cmd("post-tool-capture")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": False}]

    spy = MagicMock()
    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", spy)

    result = _probe_hook_execution(entries, timeout=2.0)
    assert result == {}
    spy.assert_not_called()


def test_timeout_passed_to_subprocess(monkeypatch):
    """The timeout argument is forwarded to subprocess.run."""
    cmd = _runner_cmd("post-tool-capture")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    spy = MagicMock(return_value=subprocess.CompletedProcess(args=[cmd], returncode=0))
    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", spy)

    _probe_hook_execution(entries, timeout=1.7)
    args, kwargs = spy.call_args
    assert kwargs.get("timeout") == 1.7


def test_payload_is_passed_as_stdin(monkeypatch):
    """The probe sends JSON via stdin so the runner can read it."""
    cmd = _runner_cmd("prompt-recall")
    entries = [{"command": cmd, "scope": "global", "path": "/x", "runner_dispatched": True}]
    spy = MagicMock(return_value=subprocess.CompletedProcess(args=[cmd], returncode=0))
    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", spy)

    _probe_hook_execution(entries, timeout=2.0)
    args, kwargs = spy.call_args
    payload = kwargs.get("input")
    assert payload is not None
    parsed = json.loads(payload)
    assert parsed["type"] == "prompt-recall"


# ── report-level integration: probe folded into verify_managed_hooks ────────


def test_ok_false_when_any_probe_failed(monkeypatch, tmp_path):
    """A registered hook that hangs in the probe flips ok to False."""
    settings = {
        "hooks": {
            "SessionStart": [
                _entry(_runner_cmd("session-start-context")),
                _entry(_runner_cmd("post-compact-rehydrate")),
            ],
        }
    }
    _write_global_settings(tmp_path, settings)

    import yadgar.core.install._verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "_harvest_expected",
        lambda home_dir, project_directory=None: (
            {
                "SessionStart": {
                    "session-start-context": True,
                    "post-compact-rehydrate": True,
                }
            },
            [],
        ),
    )

    def _classify(cmd_list, **kwargs):
        joined = " ".join(cmd_list) if isinstance(cmd_list, list) else str(cmd_list)
        if "post-compact-rehydrate" in joined:
            raise subprocess.TimeoutExpired(cmd=cmd_list, timeout=kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(args=cmd_list, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("yadgar.core.install._verify.subprocess.run", _classify)

    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is False, format_hook_verify_report(report)

    execution = report.get("execution", {})
    assert execution.get("session-start-context", {}).get("status") == "ran"
    assert execution.get("post-compact-rehydrate", {}).get("status") == "hung"


def test_ok_true_when_all_probes_ran(monkeypatch, tmp_path):
    """All registered hooks probe OK → ok stays True."""
    settings = {
        "hooks": {
            "SessionStart": [_entry(_runner_cmd("session-start-context"))],
        }
    }
    _write_global_settings(tmp_path, settings)

    import yadgar.core.install._verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "_harvest_expected",
        lambda home_dir, project_directory=None: (
            {"SessionStart": {"session-start-context": True}},
            [],
        ),
    )
    monkeypatch.setattr(
        "yadgar.core.install._verify.subprocess.run",
        _patched_subprocess("ok"),
    )

    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is True
    assert report["execution"]["session-start-context"]["status"] == "ran"


def test_probe_does_not_affect_existing_report_shape(monkeypatch, tmp_path):
    """Existing keys still parse identically — only new keys are added."""
    _write_global_settings(tmp_path, {"hooks": {}})

    import yadgar.core.install._verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "_harvest_expected",
        lambda home_dir, project_directory=None: ({}, []),
    )
    monkeypatch.setattr(
        "yadgar.core.install._verify.subprocess.run",
        _patched_subprocess("ok"),
    )

    report = verify_managed_hooks(home_dir=tmp_path)
    for key in ("ok", "scopes_inspected", "findings", "counts"):
        assert key in report, f"existing key {key!r} missing after probe"
    assert "execution" in report
