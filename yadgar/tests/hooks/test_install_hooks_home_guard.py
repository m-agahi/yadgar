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

import json
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
    regression pin (plan acceptance #1).

    Car 7 (2026-07-26): the MCP `install_hooks` tool now delegates to
    `install_client("claude-code", mcp=False, rules=False, hooks=True, ...)`
    (see yadgar/core/server/tools/misc.py::install_hooks). The return shape
    changed from the legacy `{status, settings_file}` to the orchestrator's
    `{status, scope, result: {client, mcp, rules, hooks, dry_run}}`. The
    test still pins the regression we care about (write lands under guard
    HOME, not real ~/.claude) via the per-kind emitter's write path.
    """
    monkeypatch.delenv("YADGAR_IN_CONTAINER", raising=False)
    import yadgar.core.install.install_hooks_lib as lib

    monkeypatch.setattr(lib, "is_running_in_container", lambda: False)

    from yadgar.core.server.tools import misc

    guard_home = Path.home()  # the guard's tmp HOME
    proj = guard_home / "proj"
    proj.mkdir(parents=True, exist_ok=True)

    result = misc.install_hooks(project_directory=str(proj), scope="global")

    # New return shape: {status, scope, result: {client, mcp, rules, hooks, dry_run}}
    assert result["status"] == "installed", result
    inner = result["result"]
    assert inner["client"] == "claude-code"
    hooks_block = inner["hooks"]
    assert hooks_block is not None, (
        f"MCP wrapper returned hooks=None under guard HOME; full={inner}"
    )
    settings_path = Path(hooks_block["path"])
    # The settings file MUST be under the guard's tmp HOME, not real ~/.claude.
    assert str(settings_path).startswith(str(guard_home)), (
        f"MCP wrapper wrote to {settings_path}, outside the guard HOME {guard_home}"
    )
    assert settings_path.exists()
    # The legacy test asserted the router script; the new path delegates
    # to the per-kind emitter `_emit_claude_json` which writes settings.json
    # only (router is installed by `install_hooks_impl` which is no longer
    # called from this MCP path). Pin the new artifact: settings.json exists
    # at the expected path with the expected hooks block.
    settings = json.loads(settings_path.read_text())
    assert "hooks" in settings, f"No 'hooks' key in {settings_path}"


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
