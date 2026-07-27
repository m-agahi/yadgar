"""Car 0 — hook-emitter dispatch (``hooks_render.register_hooks``).

Mirrors the ``mcp_register`` / ``rules_render`` generator shape: one public
``register_hooks(descriptor, …)`` entrypoint that dispatches on
``descriptor.hooks_kind`` to a per-kind ``_emit_<kind>()``. Car 0 makes the SEAM
live and implements ONLY the ``claude_json`` kind (routed through the shared
``install_hooks_impl`` — one code path). The other 7 kinds are typed
``NotImplementedError`` stubs the later cars fill; ``hooks_kind is None`` emits
nothing (Gemini advisory-only).
"""

from __future__ import annotations

import pytest

from yadgar.core.install.clients import hooks_render
from yadgar.core.install.clients.registry import CLIENT_REGISTRY

# The kinds later cars still leave as stubs (Car A implemented
# ``opencode_plugin``; Car B implemented ``cursor_hooks``).
_STUB_KINDS = [
    "codex_hooks_json",
    "cline_hooks",
    "windsurf_hooks",
    "kiro_hooks_json",
    "amp_hooks",
]


def test_dispatch_table_covers_every_registry_hooks_kind():
    """Every non-None hooks_kind in the registry has a dispatch entry (no gaps)."""
    kinds = {d.hooks_kind for d in CLIENT_REGISTRY.values() if d.hooks_kind is not None}
    assert kinds <= hooks_render._DISPATCHED_KINDS, (
        f"unhandled hooks_kind(s): {kinds - hooks_render._DISPATCHED_KINDS}"
    )


def test_real_emitters_are_claude_json_cursor_and_opencode():
    """Car 0: claude_json. Car A (2026-07-26): opencode_plugin. Car B: cursor_hooks. Rest stubs."""
    assert set(hooks_render._EMITTERS) == {"claude_json", "cursor_hooks", "opencode_plugin"}
    assert set(hooks_render._STUB_CARS) == {
        "codex_hooks_json",
        "cline_hooks",
        "windsurf_hooks",
        "kiro_hooks_json",
        "amp_hooks",
    }


def test_none_hooks_kind_emits_nothing():
    """Gemini (hooks_kind None) → no-op result, no raise."""
    gemini = CLIENT_REGISTRY["gemini"]
    result = hooks_render.register_hooks(gemini, home_dir=None, scope="global")
    assert result["emitted"] is False
    assert result["hooks_kind"] is None


@pytest.mark.parametrize("kind", _STUB_KINDS)
def test_stub_kinds_raise_not_implemented(kind, tmp_path):
    """The 5 later-car kinds are explicit NotImplementedError stubs (not silent)."""
    # Find a descriptor carrying this kind.
    desc = next(d for d in CLIENT_REGISTRY.values() if d.hooks_kind == kind)
    with pytest.raises(NotImplementedError):
        hooks_render.register_hooks(desc, home_dir=tmp_path, scope="global")


def test_claude_json_delegates_to_install_hooks_impl(tmp_path, monkeypatch):
    """claude_json routes through the shared install_hooks_impl (one code path)."""
    seen = {}

    def _fake_impl(home_dir, scope, project_directory, dry_run=False):
        seen["home_dir"] = home_dir
        seen["scope"] = scope
        seen["dry_run"] = dry_run
        return {"status": "installed", "scope": scope}

    monkeypatch.setattr(hooks_render, "install_hooks_impl", _fake_impl)
    cc = CLIENT_REGISTRY["claude-code"]
    result = hooks_render.register_hooks(cc, home_dir=tmp_path, scope="global")

    assert seen["home_dir"] == tmp_path
    assert seen["scope"] == "global"
    assert result["emitted"] is True
    assert result["hooks_kind"] == "claude_json"
    assert result["result"]["status"] == "installed"


def test_claude_json_dry_run_threads_through(tmp_path, monkeypatch):
    captured = {}

    def _fake_impl(home_dir, scope, project_directory, dry_run=False):
        captured["dry_run"] = dry_run
        return {"status": "dry_run"}

    monkeypatch.setattr(hooks_render, "install_hooks_impl", _fake_impl)
    cc = CLIENT_REGISTRY["claude-code"]
    hooks_render.register_hooks(cc, home_dir=tmp_path, scope="global", dry_run=True)
    assert captured["dry_run"] is True


def test_claude_json_idempotent_second_run_no_change(tmp_path):
    """2nd real run against the same settings.json is a no-op (byte-identical).

    Exercises the REAL install_hooks_impl (not mocked) to prove the delegated
    path is idempotent — the emitter's idempotency IS install_hooks_impl's.
    """
    cc = CLIENT_REGISTRY["claude-code"]
    r1 = hooks_render.register_hooks(cc, home_dir=tmp_path, scope="global")
    assert r1["emitted"] is True
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    first = settings.read_text()

    hooks_render.register_hooks(cc, home_dir=tmp_path, scope="global")
    assert settings.read_text() == first
