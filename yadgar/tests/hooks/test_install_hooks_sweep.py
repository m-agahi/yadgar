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

# ADR-0156 removed the SubagentStop append hook; the sweep now manages three.
_MANAGED_BASENAMES = {
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
    """Seed a SubagentStart yadgar entry with a bare ``python3`` interpreter,
    then install: exactly one managed entry, carrying the freshly-resolved
    (durable ``sys.executable``) interpreter, must remain."""
    # _stable_python() only returns sys.executable when it is DURABLE (not under
    # .claude/worktrees/, /tmp, or a linked git worktree). Running this suite
    # from inside an agent worktree makes the real sys.executable non-durable —
    # _stable_python then substitutes it away, and here it KEEPS the seeded bare
    # ``python3`` existing registration (non-absolute → durable, resolves on
    # PATH), yielding a command byte-identical to the stale seed. Pin a fake
    # durable interpreter so the refresh path (durable sys.executable wins) runs,
    # exactly as the shebang tests do (ADR-0092: sys.executable is non-durable in
    # worktree venvs by design).
    fake_durable = "/opt/durable-test-python/bin/python3"
    monkeypatch.setattr(sys, "executable", fake_durable)

    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    stale_cmd = f"python3 {hooks_dir}/yadgar-subagent-start.py"
    claude_dir.joinpath("settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": stale_cmd}]}
                    ]
                }
            }
        )
    )

    settings = _install_global(tmp_path, monkeypatch)
    entries = _yadgar_entries(settings["hooks"], "SubagentStart", "yadgar-subagent-start.py")
    assert len(entries) == 1, f"expected 1 managed SubagentStart entry, got {entries}"
    cmd = entries[0]["hooks"][0]["command"]
    assert cmd != stale_cmd, "stale bare-python3 entry was not refreshed"
    assert fake_durable in cmd, f"surviving command lacks the durable interpreter: {cmd!r}"


# ── sweep heals dupes (acceptance #2) ────────────────────────────────────────


def test_sweep_collapses_preexisting_duplicates(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    dup_cmd = f"python3 {hooks_dir}/yadgar-subagent-start.py"
    claude_dir.joinpath("settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SubagentStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": dup_cmd}]},
                        {"matcher": "", "hooks": [{"type": "command", "command": dup_cmd}]},
                    ]
                }
            }
        )
    )
    settings = _install_global(tmp_path, monkeypatch)
    entries = _yadgar_entries(settings["hooks"], "SubagentStart", "yadgar-subagent-start.py")
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
                    "SubagentStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": foreign_cmd}]}
                    ]
                }
            }
        )
    )
    settings = _install_global(tmp_path, monkeypatch)
    all_cmds = [
        e["hooks"][0]["command"]
        for e in settings["hooks"].get("SubagentStart", [])
        if isinstance(e, dict) and e.get("hooks")
    ]
    assert foreign_cmd in all_cmds, f"foreign hook was destroyed by the sweep: {all_cmds}"
    managed = _yadgar_entries(settings["hooks"], "SubagentStart", "yadgar-subagent-start.py")
    assert len(managed) == 1, "expected exactly one managed entry alongside the foreign one"


# ── manifest-completeness (acceptance #8) ────────────────────────────────────

# Underscore logic-modules are IMPORTED by their hyphen dispatcher, never
# copied by the installer — they are not install-intended and must be excluded.
_IMPORTED_ONLY = {
    "file_changed.py",
    "instructions_loaded.py",
    "subagent_start.py",
    # ADR-0156: subagent_stop.py + subagent-stop.py were removed with the
    # auto-store path (only subagent_start.py remains as an imported-only module).
    # findings_capture.py: shared collector imported by the pending-findings CLI +
    # session-end-capture.py; never copied/installed as a standalone hook.
    "findings_capture.py",
    # _identity_mint.py (Car C2, ADR-0227): the host-side project_id mint,
    # imported by session-start-context.py and core/cli/hook.py. It is a library
    # module, never an executable hook — it deliberately has no main() and is
    # never copied to hooks_dir.
    "_identity_mint.py",
    # task_seed.py (Car C): the mechanical harness task-list seeder, imported by
    # session-start-context.py. Same shape as _identity_mint.py — a library
    # module with no main(), never copied to hooks_dir as an executable hook.
    "task_seed.py",
}

# #64: hook_runner-dispatched core hooks. These are executed via
# ``hook_runner.py <type>``'s internal ``_HOOKS`` dict — they are NEVER copied to
# hooks_dir under any name (the old ``_copy_scope_scripts._files`` copy was pure
# vestige and is REMOVED). Distinct category from ``_IMPORTED_ONLY`` (which are
# underscore logic modules imported by a hyphen dispatcher). They lost their sole
# manifest string-literal when ``_copy_scope_scripts._files`` was deleted, so they
# must be excluded from ``install_intended`` — they are dispatched, not installed.
#
# Car 8 (bug train): "prompt-recall.py" dropped — its standalone source file
# was deleted outright (a second, unwired, project-unaware auto-recall
# implementation; the wired path is core/cli/hook.py::hook_prompt_recall).
# Its absence from `shipped` (below) already makes this a no-op for that
# name; kept accurate rather than merely harmless.
_RUNNER_DISPATCHED = {
    "post-tool-capture.py",
    "session-start-context.py",
    "pre-compact-drain.sh",
    "post-compact-rehydrate.sh",
}


def _manifest_referenced_names() -> set[str]:
    """Every source filename referenced by any surviving manifest list.

    After #64 the manifest literals live in ``_install_global_scripts`` (the
    always-global scripts), ``_install_append_hooks._append_specs`` (the 4 append
    hooks, keyed by their hyphen src name + yadgar- dst name), and the
    ``_MANAGED_NONPREFIXED`` sweep allowlist. The 4 append hooks keep their
    ``_append_specs`` src literal → they stay install-intended and referenced.

    Car C5 (ADR-0066 split) moved these literals out of the canonical
    ``install_hooks_lib.py`` into the cohesive siblings ``_settings.py``
    (``_install_global_scripts`` / ``_install_append_hooks``) and
    ``_hook_scripts.py`` (``_MANAGED_NONPREFIXED``) — scan the whole install
    package so the manifest lint follows the split.
    """
    import re

    referenced: set[str] = set()
    install_dir = _HOOKS_DIR.parent / "install"
    for src in install_dir.glob("*.py"):
        text = src.read_text()
        for m in re.finditer(r'"([A-Za-z0-9_.-]+\.(?:py|sh))"', text):
            referenced.add(m.group(1))
    return referenced


def test_manifest_references_all_install_intended_scripts():
    """Every install-intended *.py/*.sh under yadgar/core/hooks/ is referenced
    by a manifest list. Excluded: imported-only underscore modules (imported by a
    hyphen dispatcher) and hook_runner-dispatched core hooks (never copied)."""
    shipped = {
        p.name
        for p in _HOOKS_DIR.iterdir()
        if p.suffix in (".py", ".sh") and p.name != "__init__.py"
    }
    install_intended = shipped - _IMPORTED_ONLY - _RUNNER_DISPATCHED
    referenced = _manifest_referenced_names()
    missing = {n for n in install_intended if n not in referenced}
    assert not missing, f"install-intended scripts not referenced by any manifest list: {missing}"


def test_prompt_recall_standalone_script_is_removed():
    """Car 8 (bug train): yadgar/core/hooks/prompt-recall.py was a SECOND,
    unwired implementation of UserPromptSubmit auto-recall — its own raw FTS
    query, its own HTTP forward that never sent ?project=. Nothing dispatched
    to it (the wired path is _settings.py's `_runner_entry("prompt-recall")`
    -> hook_runner.py -> core/cli/hook.py::hook_prompt_recall); it was pure
    vestige. Two implementations, one wired, is exactly the drift that let a
    scoping bug (Car 2/Car 8) exist and get fixed in only one of the two
    places. Deleted rather than wired: wiring it would resurrect a second,
    competing retrieval code path Car 0 had already consolidated away."""
    assert not (_HOOKS_DIR / "prompt-recall.py").exists(), (
        "prompt-recall.py must be deleted, not merely unwired — "
        "see Car 8's decision in yadgar/core/server/http.py::hook_block_reflect's "
        "neighboring history"
    )
