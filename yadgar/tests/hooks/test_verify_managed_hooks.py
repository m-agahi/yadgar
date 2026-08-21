"""Ledger task 306 — the managed-hook wiring verification.

The defect this closes: yadgar's installer emits a set of managed hook entries
and has NO surface that answers "which of them are actually wired into the live
settings.json right now?".  Measured on this box 2026-08-21: ``PostToolUse``
carries ``post-tool-capture`` but not ``block-reflect``, and ``PreCompact`` is
an EMPTY array — so ``pre-compact-drain`` never fires either.  Two managed hooks
silently unwired, and nothing reported it, because nothing could.

TDD: these assert on the REPORT — the names it calls missing/foreign — not
merely on a non-zero exit.  An exit code that is red for the wrong reason is
the same class of lying signal this train exists to delete.

Authority boundary (task 306): the verification REPORTS divergence.  It never
edits a hook a different tool owns — nix hand-rolls the live wiring with jq
(``nix/modules/home/yadgar.nix``) using ``yadgar-``-prefixed standalone scripts
rather than yadgar's ``hook_runner.py <event>`` dispatch, and reconciling that
is task 305 in a different repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yadgar.core.install._verify import (
    _harvest_expected,
    _hook_logical_name,
    _is_runner_dispatched,
    expected_managed_hooks,
    format_hook_verify_report,
    verify_managed_hooks,
)

# ── fixtures ─────────────────────────────────────────────────────────────────

_VENVPY = "/home/user/.local/pipx/venvs/yadgar/bin/python"
_HOOKS = "/home/user/.claude/hooks"


def _entry(command: str, matcher: str = "") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _nix_shaped_settings() -> dict:
    """The live wiring on this box: nix's yadgar-<name>.py standalone scripts.

    Deliberately mirrors the real file — PostToolUse has ONE entry (no
    block-reflect) and PreCompact is an empty list.
    """
    return {
        "hooks": {
            "PreCompact": [],
            "SessionStart": [
                _entry("find /home/user/.claude/plugins/cache/caveman -name caveman.md"),
                _entry(f"{_VENVPY} {_HOOKS}/yadgar-session-start-context.py"),
                _entry(f"/bin/bash {_HOOKS}/yadgar-post-compact-rehydrate.sh", matcher="compact"),
            ],
            "PostToolUse": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-post-tool-capture.py")],
            "UserPromptSubmit": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-prompt-recall.py")],
            "PreToolUse": [
                _entry(
                    f"{_VENVPY} {_HOOKS}/yadgar-pretooluse-router.py",
                    matcher="Bash|Edit|Write|NotebookEdit",
                )
            ],
            "Stop": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-stop-memory-checkpoint.py")],
            "SessionEnd": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-session-end-capture.py")],
            "SubagentStart": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-subagent-start.py")],
            "SubagentStop": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-subagent-stop.py")],
            "InstructionsLoaded": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-instructions-loaded.py")],
            "FileChanged": [_entry(f"{_VENVPY} {_HOOKS}/yadgar-file-changed.py")],
        }
    }


def _write_global_settings(home: Path, data: dict) -> Path:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    path = claude / "settings.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def _names(report: dict, status: str) -> set[str]:
    return {f["name"] for f in report["findings"] if f["status"] == status}


# ── the normalizer: two install families, one logical name ───────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # yadgar's own shape — runner dispatch, name is the subcommand
        (f"{_VENVPY} {_HOOKS}/hook_runner.py post-tool-capture", "post-tool-capture"),
        (f"{_VENVPY} {_HOOKS}/hook_runner.py block-reflect", "block-reflect"),
        (f"{_VENVPY} {_HOOKS}/hook_runner.py pre-compact-drain", "pre-compact-drain"),
        # nix's shape — yadgar-<name>.py standalone script
        (f"{_VENVPY} {_HOOKS}/yadgar-post-tool-capture.py", "post-tool-capture"),
        (f"/bin/bash {_HOOKS}/yadgar-post-compact-rehydrate.sh", "post-compact-rehydrate"),
        # standalone scripts yadgar itself installs under a yadgar- name
        (f"{_VENVPY} {_HOOKS}/yadgar-pretooluse-router.py", "pretooluse-router"),
        (f"{_VENVPY} {_HOOKS}/yadgar-stop-memory-checkpoint.py", "stop-memory-checkpoint"),
        # foreign entries carry no yadgar identity at all
        ("find /home/user/.claude/plugins/cache/caveman -name caveman.md", None),
        ("", None),
    ],
)
def test_logical_name_spans_both_install_families(command, expected):
    assert _hook_logical_name(command) == expected


def test_runner_dispatch_is_distinguishable_from_standalone():
    assert _is_runner_dispatched(f"{_VENVPY} {_HOOKS}/hook_runner.py post-tool-capture")
    assert not _is_runner_dispatched(f"{_VENVPY} {_HOOKS}/yadgar-post-tool-capture.py")


# ── the expected set is harvested from the installer, never hand-written ─────


def test_expected_set_is_harvested_from_the_installer(tmp_path):
    expected = expected_managed_hooks(home_dir=tmp_path)
    assert expected["PostToolUse"] == {"post-tool-capture", "block-reflect"}
    assert expected["PreCompact"] == {"pre-compact-drain"}
    assert expected["SessionStart"] == {"session-start-context", "post-compact-rehydrate"}
    assert "pretooluse-router" in expected["PreToolUse"]
    assert "stop-memory-checkpoint" in expected["Stop"]
    assert "session-end-capture" in expected["SessionEnd"]


def test_harvesting_the_expected_set_writes_nothing(tmp_path):
    """The harvest runs the installer in dry_run — it must not touch disk."""
    home = tmp_path / "home"
    home.mkdir()
    expected_managed_hooks(home_dir=home)
    assert list(home.iterdir()) == [], f"harvest wrote files: {list(home.iterdir())}"


# ── the report: names what is missing, on the real-world fixture ─────────────


def test_reports_block_reflect_missing_when_post_tool_capture_is_alone(tmp_path):
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert "block-reflect" in _names(report, "missing")
    assert "post-tool-capture" not in _names(report, "missing")


def test_reports_pre_compact_drain_missing_when_precompact_is_empty(tmp_path):
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert "pre-compact-drain" in _names(report, "missing")


def test_nix_shaped_entry_counts_present_and_is_flagged_foreign(tmp_path):
    """A yadgar-<name>.py entry IS the hook — a naive command compare would
    call every one of them missing, which is the version that must not ship."""
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    foreign = _names(report, "foreign")
    assert "post-tool-capture" in foreign
    assert "prompt-recall" in foreign
    assert "post-tool-capture" not in _names(report, "missing")


def test_standalone_scripts_are_not_foreign_when_shapes_agree(tmp_path):
    """pretooluse-router is a standalone script in BOTH families — same shape,
    so it must not be smeared as foreign just because nix wrote it."""
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert "pretooluse-router" not in _names(report, "foreign")
    assert "pretooluse-router" not in _names(report, "missing")


def test_divergence_is_not_ok(tmp_path):
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is False


def test_report_text_names_the_missing_hooks(tmp_path):
    _write_global_settings(tmp_path, _nix_shaped_settings())
    text = format_hook_verify_report(verify_managed_hooks(home_dir=tmp_path))
    assert "block-reflect" in text
    assert "pre-compact-drain" in text
    assert "PostToolUse" in text


def test_report_never_claims_authority_over_foreign_entries(tmp_path):
    """Task 306: report divergence, do not offer to rewrite a foreign hook."""
    _write_global_settings(tmp_path, _nix_shaped_settings())
    text = format_hook_verify_report(verify_managed_hooks(home_dir=tmp_path)).lower()
    assert "foreign" in text
    for verb in ("repairing", "rewriting", "overwriting"):
        assert verb not in text


# ── a real yadgar install verifies clean ─────────────────────────────────────


def test_full_yadgar_install_is_clean(tmp_path, monkeypatch):
    from yadgar.core.install.install_hooks_lib import install_hooks_impl

    monkeypatch.setenv("HOME", str(tmp_path))
    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=str(tmp_path))
    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is True, format_hook_verify_report(report)
    assert _names(report, "missing") == set()
    assert _names(report, "foreign") == set()


# ── scope honesty: a hook may live in the project settings, not the global ───


def test_missing_globally_but_present_in_project_scope_is_not_missing(tmp_path):
    """ADR-0173: Claude Code merges global + project settings without dedup.
    Reporting a project-scoped hook as missing would be a lying verifier."""
    _write_global_settings(tmp_path, _nix_shaped_settings())
    proj = tmp_path / "proj" / ".claude"
    proj.mkdir(parents=True)
    (proj / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreCompact": [
                        _entry(f"{_VENVPY} {proj}/hooks/hook_runner.py pre-compact-drain")
                    ]
                }
            }
        )
    )
    report = verify_managed_hooks(home_dir=tmp_path, project_directory=str(tmp_path / "proj"))
    assert "pre-compact-drain" not in _names(report, "missing")
    found = next(f for f in report["findings"] if f["name"] == "pre-compact-drain")
    assert found["scope"] == "project"


def test_report_states_which_scopes_were_inspected(tmp_path):
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    paths = {s["path"] for s in report["scopes_inspected"]}
    assert str(tmp_path / ".claude" / "settings.json") in paths
    text = format_hook_verify_report(report)
    assert str(tmp_path / ".claude" / "settings.json") in text


def test_absent_settings_file_is_reported_not_crashed(tmp_path):
    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is False
    assert "block-reflect" in _names(report, "missing")


# ── stale wiring: a hook yadgar no longer manages ────────────────────────────


def test_yadgar_named_entry_yadgar_no_longer_installs_is_surfaced(tmp_path):
    """ADR-0156 removed the SubagentStop hook; nix still wires it here."""
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert "subagent-stop" in _names(report, "unexpected")


def test_unexpected_alone_does_not_fail_the_check(tmp_path, monkeypatch):
    from yadgar.core.install.install_hooks_lib import install_hooks_impl

    monkeypatch.setenv("HOME", str(tmp_path))
    install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=str(tmp_path))
    settings_path = tmp_path / ".claude" / "settings.json"
    data = json.loads(settings_path.read_text())
    data["hooks"]["SubagentStop"] = [_entry(f"{_VENVPY} {_HOOKS}/yadgar-subagent-stop.py")]
    settings_path.write_text(json.dumps(data))
    report = verify_managed_hooks(home_dir=tmp_path)
    assert "subagent-stop" in _names(report, "unexpected")
    assert report["ok"] is True


# ── placement: the check must live somewhere that actually runs ──────────────


def _setup_script() -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "scripts" / "install" / "yadgar-setup.sh").read_text()


def test_doctor_probes_managed_hook_wiring():
    """A check nobody runs is the same defect one layer up.

    ``yadgar-setup --doctor`` is the established host-side verification
    surface (it already probes launchd/systemd units, the host CLI path and
    the metrics endpoint).  The hook-wiring check belongs beside them.
    """
    text = _setup_script()
    assert "verify-hooks" in text, "doctor never probes managed-hook wiring"
    assert "_probe_managed_hooks" in text


def test_doctor_hook_probe_is_called_from_run_doctor():
    text = _setup_script()
    doctor = text.split("_run_doctor() {", 1)[1]
    assert "_probe_managed_hooks" in doctor, "_probe_managed_hooks defined but never called"


def test_doctor_hook_probe_never_repairs():
    """The probe reports; repairing a foreign-installed hook is not yadgar's
    call (task 305, different repo)."""
    text = _setup_script()
    probe = text.split("_probe_managed_hooks() {", 1)[1].split("\n}", 1)[0]
    assert "install --client" not in probe
    assert "--hooks" not in probe


# ── drift must be loud, never a silently smaller expected set ────────────────


def test_installer_preview_yields_no_unrecognized_command(tmp_path):
    """Every command yadgar's own installer emits must be nameable.

    A managed hook whose command matches neither install family would drop out
    of the expected set — and the verifier would then report CLEAN for a hook
    it cannot see, which is the exact silence this module exists to end.  The
    shape is reachable: ``core/cli/hook.py`` documents a ``yadgar hook <event>``
    dispatch used to wire ported clients, and that spelling is nameable by
    neither family.  One emitter change and the check goes blind at exit 0
    unless this stays loud.
    """
    _expected, unrecognized = _harvest_expected(home_dir=tmp_path)
    assert unrecognized == [], f"installer emits un-nameable commands: {unrecognized}"


def test_unrecognized_installer_command_fails_the_check(tmp_path, monkeypatch):
    import yadgar.core.install._verify as verify_mod

    real = verify_mod._hook_logical_name

    def _blind_to_block_reflect(command: str):
        return None if command.endswith("block-reflect") else real(command)

    monkeypatch.setattr(verify_mod, "_hook_logical_name", _blind_to_block_reflect)
    _write_global_settings(tmp_path, _nix_shaped_settings())
    report = verify_managed_hooks(home_dir=tmp_path)
    assert report["ok"] is False
    assert any(f["status"] == "unrecognized" for f in report["findings"])
    text = format_hook_verify_report(report)
    assert "unrecognized" in text.lower()
    assert "block-reflect" in text
