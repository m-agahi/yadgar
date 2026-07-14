"""Content-hash orphan-sweep tests (#64, Part 2).

The sweep removes vestigial NON-prefixed hook copies (`post-tool-capture.py`
et al.) that prior global installs wrote into ~/.claude/hooks/ and nothing
dispatches to (hook_runner dispatches internally via `_HOOKS`, never execs a
sibling). Predicate = CONTENT-HASH vs the packaged source — the ONLY signal
that works for the 5 runner-dispatched names (which have NO `yadgar-`-prefixed
on-disk sibling). A user's coincidentally-named file with different content
survives; a nix-store symlink survives (provenance skip).

Root-cause half: a clean global install no longer EMITS the 9 non-prefixed
names in the first place.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yadgar.core.install.install_hooks_lib as lib

_PACKAGE_HOOKS = Path(lib.__file__).parents[1] / "hooks"

# The 9 non-prefixed names prior installs emitted (the sweep's target set).
_MANAGED_NONPREFIXED = {
    "pre-compact-drain.sh",
    "post-compact-rehydrate.sh",
    "post-tool-capture.py",
    "session-start-context.py",
    "prompt-recall.py",
    "subagent-stop.py",
    "instructions-loaded.py",
    "subagent-start.py",
    "file-changed.py",
}

# The 5 with NO `yadgar-`-prefixed on-disk sibling — the ones a sibling-existence
# predicate would silently miss.
_RUNNER_DISPATCHED = {
    "post-tool-capture.py",
    "session-start-context.py",
    "prompt-recall.py",
    "pre-compact-drain.sh",
    "post-compact-rehydrate.sh",
}


def _install_global(tmp_path, monkeypatch):
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)
    lib.install_hooks_impl(home_dir=tmp_path, scope="global", project_directory=None, dry_run=False)
    return tmp_path / ".claude" / "hooks"


def _seed_packaged_copy(hooks_dir: Path, name: str) -> None:
    """Write a byte-identical copy of the packaged source (a real yadgar orphan)."""
    src = _PACKAGE_HOOKS / name
    (hooks_dir / name).write_bytes(src.read_bytes())


# ── root cause: clean install emits zero non-prefixed names (acceptance #7) ──


def test_clean_install_emits_no_nonprefixed_names(tmp_path, monkeypatch):
    hooks_dir = _install_global(tmp_path, monkeypatch)
    present = {p.name for p in hooks_dir.iterdir()}
    leaked = present & _MANAGED_NONPREFIXED
    assert not leaked, f"clean global install emitted non-prefixed vestige names: {leaked}"


# ── sweep removes ALL 9 real orphans incl. the 5 runner-dispatched (accept #3) ──


def test_sweep_removes_all_nine_real_orphans(tmp_path, monkeypatch):
    """Seed the 9 non-prefixed names as BYTE-IDENTICAL copies of the packaged
    source (exactly what a prior install produced — NO fabricated `yadgar-`
    siblings for the 5 core names). A global install must sweep all 9,
    including the 5 runner-dispatched names a sibling-existence predicate misses."""
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    for name in _MANAGED_NONPREFIXED:
        _seed_packaged_copy(hooks_dir, name)

    _install_global(tmp_path, monkeypatch)

    survivors = {p.name for p in hooks_dir.iterdir()} & _MANAGED_NONPREFIXED
    assert not survivors, f"orphan non-prefixed copies survived the sweep: {survivors}"
    # The 5 runner-dispatched specifically — the regression the audit flagged.
    missed = {p.name for p in hooks_dir.iterdir()} & _RUNNER_DISPATCHED
    assert not missed, f"runner-dispatched orphans NOT swept (D1 regression): {missed}"


# ── sweep preserves a user's same-name-different-content file (acceptance #4) ──


def test_sweep_preserves_foreign_same_name_core_hook(tmp_path, monkeypatch):
    """A user file named `post-tool-capture.py` (a RUNNER-dispatched name — no
    `yadgar-` sibling) whose CONTENT differs from the packaged source must
    survive. Discriminates the content-hash predicate from a naive name
    allowlist AND from sibling-existence."""
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    foreign = hooks_dir / "post-tool-capture.py"
    foreign_body = "#!/usr/bin/env python3\n# user's own hook, NOT yadgar's\nprint('mine')\n"
    foreign.write_text(foreign_body)

    _install_global(tmp_path, monkeypatch)

    assert foreign.exists(), "user's same-name different-content file was destroyed"
    assert foreign.read_text() == foreign_body, "user's file content was mutated"


# ── sweep preserves a nix-store symlink (provenance skip) ────────────────────


def test_sweep_preserves_nix_store_symlink(tmp_path, monkeypatch):
    """A nix-deployed hook is a symlink into /nix/store/. Even if it would be
    byte-identical, the sweep must NOT unlink it — that would fight
    home-manager. Uses a DANGLING symlink so the test needs no real /nix/store."""
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    nix_link = hooks_dir / "session-start-context.py"
    nix_link.symlink_to("/nix/store/deadbeef-yadgar-hooks/session-start-context.py")

    _install_global(tmp_path, monkeypatch)

    assert nix_link.is_symlink(), "nix-store symlink was unlinked by the sweep"
    assert os.readlink(nix_link).startswith("/nix/store"), "nix symlink target changed"


# ── db-lockdown orphan still unlinked (acceptance #5, regression) ────────────


def test_db_lockdown_orphan_unlinked_via_sweep(tmp_path, monkeypatch):
    claude_dir = tmp_path / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    orphan = hooks_dir / "yadgar-db-lockdown-check.py"
    orphan.write_text("# stale\n")
    _install_global(tmp_path, monkeypatch)
    assert not orphan.exists()


# ── idempotent convergence (acceptance #6) ──────────────────────────────────


def test_reinstall_converges_zero_nonprefixed(tmp_path, monkeypatch):
    """Two consecutive global installs leave zero non-prefixed dupes and zero
    db-lockdown orphan — a clean managed set only."""
    _install_global(tmp_path, monkeypatch)
    hooks_dir = _install_global(tmp_path, monkeypatch)
    present = {p.name for p in hooks_dir.iterdir()}
    assert not (present & _MANAGED_NONPREFIXED), f"non-prefixed dupes after reinstall: {present}"
    assert "yadgar-db-lockdown-check.py" not in present
    # Managed set intact.
    assert "hook_runner.py" in present
    assert "yadgar-pretooluse-router.py" in present


def test_packaged_sources_exist_for_all_managed_names():
    """Guard: every name in the sweep allowlist has a packaged source to hash
    against — else the content-hash predicate silently no-ops."""
    for name in _MANAGED_NONPREFIXED:
        src = _PACKAGE_HOOKS / name
        assert src.exists(), f"packaged source missing for sweep target {name}"
        # sanity: the hash is stable/readable
        assert hashlib.sha256(src.read_bytes()).hexdigest()
