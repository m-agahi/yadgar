"""Hook-install HOME-isolation guard tests (#64, Part 1).

The 2026-07-13 incident: an in-session install_hooks resolving
`home_dir=Path.home()` unlinked ~/.claude/hooks/yadgar-db-lockdown-check.py and
blocked Bash. The latent risk: any FUTURE test calling the MCP/CLI wrapper
(both hardcode `home_dir=Path.home()`) or `sync_instructions` WITHOUT a per-test
HOME patch would write to the developer's real ~/.claude. The conftest guard
(`isolate_yadgar_paths` + `_isolate_yadgar_paths_session`) redirects HOME to a
tmp dir for the WHOLE suite, closing that path by construction. These tests are
the regression pins for that redirect, plus the sentinel tripwire.
"""

from __future__ import annotations

import os
from pathlib import Path


def test_path_home_is_redirected_to_tmp_not_real_home():
    """The autouse conftest guard redirects HOME so `Path.home()` inside ANY
    test resolves to a tmp dir — never the developer's real home."""
    resolved = Path.home()
    real_home_marker = Path("/home") / os.environ.get("USER", "nobody-xyz")
    assert resolved != real_home_marker, (
        f"Path.home() resolved to a real-looking home {resolved} — HOME guard absent"
    )
    # The guard's per-test HOME ends in .../_guard_home (function fixture) —
    # assert we are under a tmp path, not a persistent home.
    assert resolved.name == "_guard_home", (
        f"Path.home()={resolved} is not the guard's tmp home subdir"
    )


def test_unpatched_mcp_wrapper_install_lands_under_tmp_home(monkeypatch):
    """Call the REAL (unmocked) MCP-server install wrapper with NO per-test HOME
    patch. Without the conftest guard this would write to real ~/.claude; with
    it, the write is redirected to the guard's tmp HOME. This is the latent-leak
    regression pin (plan acceptance #1)."""
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    import yadgar.core.install.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    from yadgar.core.server.tools import misc

    guard_home = Path.home()  # the guard's tmp HOME
    proj = guard_home / "proj"
    proj.mkdir(parents=True, exist_ok=True)

    result = misc.install_hooks(project_directory=str(proj), scope="global")

    assert result["status"] == "installed", result
    settings_file = Path(result["settings_file"])
    # The settings file MUST be under the guard's tmp HOME, not real ~/.claude.
    assert str(settings_file).startswith(str(guard_home)), (
        f"install wrote to {settings_file}, outside the guard HOME {guard_home}"
    )
    assert settings_file.exists()
    router = guard_home / ".claude" / "hooks" / "yadgar-pretooluse-router.py"
    assert router.exists(), "router script not installed under tmp HOME"


def test_sentinel_snapshot_detects_child_mutation(tmp_path):
    """Prove the sentinel's snapshot/compare logic FIRES on a child-file change.

    The real-HOME sentinel is exists()-guarded read-only against ~/.claude/hooks/
    (never mutated, never testable directly). This exercises the same
    `_snapshot_hooks_dir` primitive against a TMP dir: snapshot, mutate a child
    (rewrite → new size/mtime; unlink → removed entry), assert the comparison
    detects drift. Guards against an always-equal bug that would give false
    assurance."""
    from yadgar.tests.conftest import _snapshot_hooks_dir

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "a.py").write_text("original\n")
    (hooks / "b.py").write_text("keep\n")
    before = _snapshot_hooks_dir(hooks)

    # Unchanged dir → snapshots equal (no false positive).
    assert _snapshot_hooks_dir(hooks) == before

    # Rewrite a child (content/size changes) → drift detected.
    (hooks / "a.py").write_text("MUTATED-longer-content\n")
    assert _snapshot_hooks_dir(hooks) != before

    # Unlink a child → drift detected.
    (hooks / "a.py").write_text("original\n")  # restore size...
    (hooks / "b.py").unlink()
    assert _snapshot_hooks_dir(hooks) != before


def test_default_home_dir_impl_never_resolves_to_real_home(monkeypatch):
    """`install_hooks_impl(home_dir=Path.home(), ...)` — the exact call the
    wrapper/CLI make — resolves `Path.home()` to the guard's tmp HOME (plan
    acceptance #2)."""
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    import yadgar.core.install.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    guard_home = Path.home()
    proj = guard_home / "proj2"
    proj.mkdir(parents=True, exist_ok=True)

    result = lib.install_hooks_impl(
        home_dir=Path.home(), scope="global", project_directory=str(proj)
    )
    assert result["status"] == "installed", result
    assert str(Path(result["settings_file"])).startswith(str(guard_home))
