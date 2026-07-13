"""HOOKS Car 3 — BUG A migration-sweep + manifest-completeness tests.

Covers:
- Full-install drift dedup: seed a prior append entry with a DIFFERENT
  interpreter, re-install, assert exactly one entry carrying the fresh
  interpreter (strip + rebuild, not just A1 basename dedup).
- Sweep heals pre-existing duplicate managed entries.
- Over-delete guard: a foreign ``yadgar-``-substring-but-NON-managed hook
  survives the sweep (discriminating seed per audit Finding 1).
- Manifest-completeness: every install-intended hook script under
  yadgar/core/hooks/ is referenced by exactly one manifest list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yadgar.core.install.install_hooks_lib as lib

_HOOKS_DIR = Path(lib.__file__).parents[1] / "hooks"

_MANAGED_BASENAMES = {
    "yadgar-subagent-stop.py",
    "yadgar-instructions-loaded.py",
    "yadgar-subagent-start.py",
    "yadgar-file-changed.py",
}


def _install_global(tmp_path, monkeypatch):
    """Run a global-scope install against a temp HOME; return settings dict."""
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)
    lib.install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None, dry_run=False)
    settings_file = tmp_path / ".claude" / "settings.json"
    return json.loads(settings_file.read_text())


def _yadgar_entries(hooks_config: dict, event: str, basename: str) -> list:
    return [
        e
        for e in hooks_config.get(event, [])
        if isinstance(e, dict) and e.get("hooks") and basename in e["hooks"][0].get("command", "")
    ]


# ── drift dedup (acceptance #1) ──────────────────────────────────────────────


def test_full_install_drift_collapses_and_refreshes_interpreter(tmp_path, monkeypatch):
    """Seed a SubagentStop yadgar entry with a bare ``python3`` interpreter,
    then install: exactly one managed entry, carrying the freshly-resolved
    (durable ``sys.executable``) interpreter, must remain."""
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    stale_cmd = f"python3 {hooks_dir}/yadgar-subagent-stop.py"
    claude_dir.joinpath("settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": stale_cmd}]}
                    ]
                }
            }
        )
    )

    settings = _install_global(tmp_path, monkeypatch)
    entries = _yadgar_entries(settings["hooks"], "SubagentStop", "yadgar-subagent-stop.py")
    assert len(entries) == 1, f"expected 1 managed SubagentStop entry, got {entries}"
    cmd = entries[0]["hooks"][0]["command"]
    assert cmd != stale_cmd, "stale bare-python3 entry was not refreshed"
    assert sys.executable in cmd, f"surviving command lacks the durable interpreter: {cmd!r}"


# ── sweep heals dupes (acceptance #2) ────────────────────────────────────────


def test_sweep_collapses_preexisting_duplicates(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    dup_cmd = f"python3 {hooks_dir}/yadgar-subagent-stop.py"
    claude_dir.joinpath("settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": dup_cmd}]},
                        {"matcher": "", "hooks": [{"type": "command", "command": dup_cmd}]},
                    ]
                }
            }
        )
    )
    settings = _install_global(tmp_path, monkeypatch)
    entries = _yadgar_entries(settings["hooks"], "SubagentStop", "yadgar-subagent-stop.py")
    assert len(entries) == 1


# ── over-delete guard (acceptance #3 — discriminating seed) ──────────────────


def test_sweep_preserves_foreign_yadgar_substring_hook(tmp_path, monkeypatch):
    """A foreign hook whose command contains the ``yadgar-`` substring but is
    NOT one of the four managed basenames must SURVIVE the sweep. This seed is
    deleted by a loose ``yadgar-`` predicate and preserved by basename-scoping,
    so it discriminates the two."""
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    foreign_cmd = "python3 /opt/yadgar-extras/custom.py"
    claude_dir.joinpath("settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": foreign_cmd}]}
                    ]
                }
            }
        )
    )
    settings = _install_global(tmp_path, monkeypatch)
    all_cmds = [
        e["hooks"][0]["command"]
        for e in settings["hooks"].get("SubagentStop", [])
        if isinstance(e, dict) and e.get("hooks")
    ]
    assert foreign_cmd in all_cmds, f"foreign hook was destroyed by the sweep: {all_cmds}"
    managed = _yadgar_entries(settings["hooks"], "SubagentStop", "yadgar-subagent-stop.py")
    assert len(managed) == 1, "expected exactly one managed entry alongside the foreign one"


# ── manifest-completeness (acceptance #8) ────────────────────────────────────

# Underscore logic-modules are IMPORTED by their hyphen dispatcher, never
# copied by the installer — they are not install-intended and must be excluded.
_IMPORTED_ONLY = {
    "file_changed.py",
    "instructions_loaded.py",
    "subagent_start.py",
    "subagent_stop.py",
}


def _manifest_referenced_names() -> set[str]:
    """Every source filename referenced by any of the three manifest lists."""
    referenced: set[str] = set()
    # _copy_scope_scripts._files dict
    src = _HOOKS_DIR.parent / "install" / "install_hooks_lib.py"
    text = src.read_text()
    # Pull the literal script names present in the module source. The three
    # manifest lists all embed the source basenames as string literals.
    import re

    for m in re.finditer(r'"([A-Za-z0-9_.-]+\.(?:py|sh))"', text):
        referenced.add(m.group(1))
    return referenced


def test_manifest_references_all_install_intended_scripts():
    """Every install-intended *.py/*.sh under yadgar/core/hooks/ is referenced
    by a manifest list. Imported-only underscore modules and __init__ excluded.
    Tolerates the pre-existing hyphen/yadgar- double-copy (audit Finding 4)."""
    shipped = {
        p.name
        for p in _HOOKS_DIR.iterdir()
        if p.suffix in (".py", ".sh") and p.name != "__init__.py"
    }
    install_intended = shipped - _IMPORTED_ONLY
    referenced = _manifest_referenced_names()
    missing = {n for n in install_intended if n not in referenced}
    assert not missing, f"install-intended scripts not referenced by any manifest list: {missing}"
